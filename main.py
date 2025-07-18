"""
MarketDataPublisher Server - Real-time Orderbook Data Service
"""

import asyncio
import json
import logging
import signal
import time
from datetime import datetime
from typing import List, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import ServerConfig
from models import ServerStatus, HeartbeatMessage
from utils.logger import setup_logger
from data_loader import MarketDataLoader, OrderbookParser, DataPublisher
from queue_processor import MessageQueueProcessor
from metrics import metrics_collector

# Setup logging
logger = setup_logger(__name__)

# Create FastAPI app
app = FastAPI(
    title="MarketDataPublisher",
    description="Real-time orderbook data distribution service",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3001", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global server state
server_start_time = time.time()
active_connections: List[WebSocket] = []
total_messages_processed = 0
current_scenario = ServerConfig.INITIAL_SCENARIO

# Initialize components
data_loader = MarketDataLoader()
orderbook_parser = OrderbookParser()
data_publisher = DataPublisher(data_loader, orderbook_parser)
queue_processor = MessageQueueProcessor()

class ConnectionManager:
    """Manage WebSocket connections"""
    
    def __init__(self):
        self.active_connections: List[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        """Connect a new client"""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"Client connected. Total clients: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        """Disconnect a client"""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"Client disconnected. Total clients: {len(self.active_connections)}")
    
    async def send_personal_message(self, message: str, websocket: WebSocket):
        """Send message to specific client"""
        try:
            await websocket.send_text(message)
        except Exception as e:
            logger.error(f"Error sending message to client: {e}")
            self.disconnect(websocket)
    
    async def broadcast(self, message: str):
        """Broadcast message to all connected clients"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                disconnected.append(connection)
        
        # Remove disconnected clients
        for connection in disconnected:
            self.disconnect(connection)

# Initialize connection manager
manager = ConnectionManager()

# Global shutdown event
server_shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, initiating shutdown...")
    server_shutdown_event.set()
    # Force exit after a short delay to prevent hanging
    import threading
    def force_exit():
        import time
        time.sleep(2)
        import os
        os._exit(0)
    threading.Thread(target=force_exit, daemon=True).start()

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Callback functions for queue processor
async def handle_orderbook_processed(orderbook_data: dict):
    """Handle processed orderbook data"""
    global total_messages_processed
    total_messages_processed += 1
    
    # Broadcast to all connected clients
    message = {
        "type": "orderbook_update",
        "data": orderbook_data,
        "timestamp": datetime.utcnow().isoformat()
    }
    await manager.broadcast(json.dumps(message))

async def handle_heartbeat(heartbeat: HeartbeatMessage):
    """Handle heartbeat from queue processor"""
    # Update active clients count
    heartbeat.active_clients = len(manager.active_connections)
    
    # Convert heartbeat to JSON-serializable format
    heartbeat_data = {
        "timestamp": heartbeat.timestamp.isoformat(),
        "server_status": heartbeat.server_status,
        "queue_size": heartbeat.queue_size,
        "memory_usage_mb": heartbeat.memory_usage_mb,
        "active_clients": heartbeat.active_clients,
        "current_scenario": heartbeat.current_scenario,
        "processing_delay_ms": queue_processor._get_processing_delay() if queue_processor else 0,
        "uptime_seconds": time.time() - server_start_time
    }
    
    # Broadcast heartbeat to all clients
    message = {
        "type": "heartbeat",
        "data": heartbeat_data,
        "timestamp": datetime.utcnow().isoformat()
    }
    await manager.broadcast(json.dumps(message))

async def handle_incident_alert(incident_data: dict):
    """Handle system alert from queue processor"""
    # Broadcast system alert to all clients
    message = {
        "type": "incident_alert",
        "data": incident_data,
        "timestamp": datetime.utcnow().isoformat()
    }
    await manager.broadcast(json.dumps(message))
    
    logger.warning(f"System alert broadcasted: {incident_data['type']}")

@app.on_event("startup")
async def startup_event():
    """Server startup event"""
    global server_start_time, data_loader, queue_processor, publishing_running, publishing_task
    server_start_time = time.time()
    logger.info("MarketDataPublisher server starting")
    logger.info(f"Server listening on {ServerConfig.HOST}:{ServerConfig.PORT}")
    
    # Load all market scenarios
    if data_loader.load_all_scenarios():
        logger.info("Market scenarios loaded")
    else:
        logger.error("Failed to load market scenarios")
    
    # Set up queue processor callbacks
    queue_processor.set_callbacks(
        on_orderbook_processed=handle_orderbook_processed,
        on_heartbeat=handle_heartbeat,
        on_incident_alert=handle_incident_alert
    )
    
    # Start queue processor
    await queue_processor.start()
    logger.info("Queue processor started")
    
    # Auto-start data publishing
    publishing_running = True
    publishing_task = asyncio.create_task(run_data_publishing())
    logger.info("Data publishing started automatically")
    
    # Start shutdown monitor
    asyncio.create_task(_monitor_shutdown())

@app.on_event("shutdown")
async def shutdown_event():
    """Server shutdown event"""
    global queue_processor, publishing_running, publishing_task
    logger.info("MarketDataPublisher server shutting down...")
    
    # Stop data publishing
    publishing_running = False
    if publishing_task and not publishing_task.done():
        publishing_task.cancel()
    logger.info("Data publishing stopped")
    
    # Stop queue processor
    if queue_processor:
        await queue_processor.stop()
        logger.info("Queue processor stopped")

async def _monitor_shutdown():
    """Monitor for shutdown signal and gracefully stop the server"""
    await server_shutdown_event.wait()
    logger.info("Shutdown signal received, stopping server...")
    
    # Stop the queue processor
    if queue_processor:
        await queue_processor.stop()
    
    # Close all WebSocket connections
    for connection in manager.active_connections[:]:
        try:
            await connection.close()
        except Exception as e:
            logger.error(f"Error closing connection: {e}")
    
    logger.info("Server shutdown complete")

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "MarketDataPublisher Server",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/test")
async def test_endpoint():
    """Test endpoint for debugging"""
    logger.info("Test endpoint called")
    return {
        "message": "Test endpoint working",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/metrics")
async def get_metrics():
    """Prometheus metrics endpoint"""
    return Response(
        content=metrics_collector.get_metrics(),
        media_type="text/plain"
    )

@app.get("/metrics/summary")
async def get_metrics_summary():
    """Human-readable metrics summary"""
    return metrics_collector.get_metrics_summary()

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    global queue_processor
    uptime = time.time() - server_start_time
    
    # Get queue processor status
    processor_status = queue_processor.get_status() if queue_processor else {}
    
    status = ServerStatus(
        status="healthy" if not processor_status.get("incident_triggered", False) else "degraded",
        uptime_seconds=uptime,
        total_messages_processed=processor_status.get("total_messages_processed", 0),
        current_queue_size=processor_status.get("queue_size", 0),
        memory_usage_mb=processor_status.get("memory_usage_mb", 0.0),
        active_clients=len(manager.active_connections),
        current_scenario=processor_status.get("current_scenario", current_scenario),
        last_heartbeat=datetime.utcnow()
    )
    
    return status.dict()

@app.get("/status")
async def server_status():
    """Detailed server status"""
    uptime = time.time() - server_start_time
    
    status = ServerStatus(
        status="running",
        uptime_seconds=uptime,
        total_messages_processed=total_messages_processed,
        current_queue_size=0,
        memory_usage_mb=0.0,
        active_clients=len(manager.active_connections),
        current_scenario=current_scenario,
        last_heartbeat=datetime.utcnow()
    )
    
    return {
        "server": status.dict(),
        "config": {
            "host": ServerConfig.HOST,
            "port": ServerConfig.PORT,
            "max_queue_size": ServerConfig.MAX_QUEUE_SIZE,
            "processing_delay_ms": ServerConfig.PROCESSING_DELAY_MS,
            "heartbeat_interval": ServerConfig.HEARTBEAT_INTERVAL,
            "memory_threshold_mb": ServerConfig.MEMORY_THRESHOLD_MB
        }
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time data"""
    await manager.connect(websocket)
    
    try:
        # Send initial connection message
        welcome_message = {
            "type": "connection",
            "data": {
                "message": "Connected to MarketDataPublisher",
                "timestamp": datetime.utcnow().isoformat(),
                "scenario": current_scenario
            }
        }
        await manager.send_personal_message(json.dumps(welcome_message), websocket)
        
        # Keep connection alive
        while True:
            try:
                # Wait for client messages (ping/pong)
                data = await websocket.receive_text()
                logger.debug(f"Received from client: {data}")
                
                # Echo back for now (will be enhanced with actual data processing)
                response = {
                    "type": "echo",
                    "data": {
                        "message": f"Received: {data}",
                        "timestamp": datetime.utcnow().isoformat()
                    }
                }
                await manager.send_personal_message(json.dumps(response), websocket)
                
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                break
                
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
    finally:
        manager.disconnect(websocket)

@app.get("/config/profiles")
async def list_profiles():
    """List available market scenarios"""
    return {
        "available_profiles": list(ServerConfig.SCENARIOS.keys()),
        "current_profile": current_scenario,
        "profile_configs": ServerConfig.SCENARIOS
    }

@app.post("/config/profile/{profile_name}")
async def switch_profile(profile_name: str):
    """Switch to a different market scenario"""
    global current_scenario, queue_processor, publishing_task, data_publisher, publishing_running
    
    logger.info(f"Received profile switch request: {profile_name}")
    
    if profile_name not in ServerConfig.SCENARIOS:
        logger.error(f"Profile '{profile_name}' not found. Available: {list(ServerConfig.SCENARIOS.keys())}")
        raise HTTPException(status_code=400, detail=f"Profile '{profile_name}' not found")
    
    current_scenario = profile_name
    
    # Switch profile in queue processor
    if queue_processor:
        queue_processor.switch_scenario(profile_name)
    
    # Switch profile in data publisher (via data loader)
    if data_publisher and data_publisher.data_loader:
        data_publisher.data_loader.switch_scenario(profile_name)
    
    # Restart the publishing task with the new scenario
    if publishing_task and not publishing_task.done():
        publishing_task.cancel()
        logger.info("Cancelled previous publishing task")
        
        # Wait a brief moment for the cancellation to complete
        try:
            await asyncio.wait_for(publishing_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.info("Publishing task cancellation completed")
    
    # Set publishing flag to True and start new publishing task
    publishing_running = True
    publishing_task = asyncio.create_task(run_data_publishing())
    logger.info(f"Started new publishing task for scenario: {profile_name}")
    
    logger.info(f"Successfully switched to profile: {profile_name}")
    
    return {
        "message": f"Switched to profile: {profile_name}",
        "profile": profile_name,
        "timestamp": datetime.utcnow().isoformat()
    }

# Global data publishing control
publishing_running = False
publishing_task = None

async def run_data_publishing():
    """Run the data publishing"""
    global data_publisher, queue_processor, publishing_running
    
    try:
        logger.info(f"Starting data publishing for scenario: {current_scenario}")
        async for orderbook in data_publisher.start_publishing(current_scenario, speed_multiplier=2.0, loop_continuously=True):
            if not publishing_running:
                logger.info("Data publishing stopped by request")
                break
            # Add orderbook to queue for processing
            await queue_processor.add_orderbook(orderbook)
            
    except asyncio.CancelledError:
        logger.info("Publishing task cancelled")
        raise  # Re-raise CancelledError to ensure proper cleanup
    except Exception as e:
        logger.error(f"Error in data publishing: {e}")
    finally:
        publishing_running = False
        logger.info("Data publishing task finished")

@app.get("/status/publisher")
async def get_publisher_status():
    """Get data publisher status"""
    global data_publisher
    
    status = data_publisher.get_publishing_status()
    return {
        "publisher": status,
        "profile_info": data_loader.get_current_scenario_info(),
        "timestamp": datetime.utcnow().isoformat()
    }



if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=ServerConfig.HOST,
        port=ServerConfig.PORT,
        reload=ServerConfig.DEBUG,
        log_level=ServerConfig.LOG_LEVEL.lower()
    ) 