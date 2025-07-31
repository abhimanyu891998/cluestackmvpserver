"""
MarketDataPublisher Server - Real-time Orderbook Data Service
"""

import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone
from utils.datetime_utils import utc_now, utc_timestamp
from typing import List, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
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
    allow_origins=ServerConfig.CORS_ORIGINS,
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
    """Manage WebSocket and SSE connections"""
    
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.sse_clients: List[asyncio.Queue] = []
    
    async def connect(self, websocket: WebSocket):
        """Connect a new WebSocket client"""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected. Total clients: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        """Disconnect a WebSocket client"""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"WebSocket client disconnected. Total clients: {len(self.active_connections)}")
        else:
            logger.debug("Attempted to disconnect WebSocket client that was not in active connections")
    
    def add_sse_client(self, client_queue: asyncio.Queue):
        """Add a new SSE client"""
        self.sse_clients.append(client_queue)
        logger.info(f"SSE client connected. Total SSE clients: {len(self.sse_clients)}")
    
    def remove_sse_client(self, client_queue: asyncio.Queue):
        """Remove an SSE client"""
        if client_queue in self.sse_clients:
            self.sse_clients.remove(client_queue)
            logger.info(f"SSE client disconnected. Total SSE clients: {len(self.sse_clients)}")
    
    async def send_personal_message(self, message: str, websocket: WebSocket):
        """Send message to specific WebSocket client"""
        if websocket not in self.active_connections:
            logger.debug("Attempted to send message to disconnected client")
            return False
            
        try:
            await websocket.send_text(message)
            return True
        except Exception as e:
            logger.debug(f"Error sending message to client: {e}")
            self.disconnect(websocket)
            return False
    
    async def broadcast(self, message: str):
        """Broadcast message to all connected clients (WebSocket + SSE)"""
        # Broadcast to WebSocket clients
        if self.active_connections:
            disconnected = []
            for connection in self.active_connections[:]:
                try:
                    await connection.send_text(message)
                except Exception as e:
                    logger.debug(f"Error broadcasting to WebSocket client: {e}")
                    disconnected.append(connection)
            
            for connection in disconnected:
                self.disconnect(connection)
        
        # Broadcast to SSE clients
        if self.sse_clients:
            disconnected_sse = []
            for client_queue in self.sse_clients[:]:
                try:
                    await client_queue.put(message)
                except Exception as e:
                    logger.debug(f"Error broadcasting to SSE client: {e}")
                    disconnected_sse.append(client_queue)
            
            for client_queue in disconnected_sse:
                self.remove_sse_client(client_queue)
    
    def get_total_clients(self) -> int:
        """Get total number of connected clients (WebSocket + SSE)"""
        return len(self.active_connections) + len(self.sse_clients)

# Initialize connection manager
manager = ConnectionManager()

# Global shutdown event
server_shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, initiating shutdown...")
    
    # Stop metrics collection
    metrics_collector.stop()
    
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
        "timestamp": utc_timestamp()
    }
    await manager.broadcast(json.dumps(message))

async def handle_heartbeat(heartbeat: HeartbeatMessage):
    """Handle heartbeat from queue processor"""
    # Update active clients count
    heartbeat.active_clients = manager.get_total_clients()
    
    # Convert heartbeat to JSON-serializable format
    heartbeat_data = {
        "timestamp": heartbeat.timestamp.isoformat(),
        "server_status": heartbeat.server_status,
        "queue_size": heartbeat.queue_size,
        "memory_usage_mb": heartbeat.memory_usage_mb,
        "active_clients": heartbeat.active_clients,
        "current_scenario": heartbeat.current_scenario,
        "processing_delay_ms": queue_processor._get_processing_delay() if queue_processor else 0,
        "uptime_seconds": time.time() - server_start_time,
        "total_messages_received": queue_processor.total_messages_received if queue_processor else 0
    }
    
    # Broadcast heartbeat to all clients
    message = {
        "type": "heartbeat",
        "data": heartbeat_data,
        "timestamp": utc_timestamp()
    }
    await manager.broadcast(json.dumps(message))

async def handle_incident_alert(incident_data: dict):
    """Handle system alert from queue processor"""
    # Broadcast system alert to all clients
    message = {
        "type": "incident_alert",
        "data": incident_data,
        "timestamp": utc_timestamp()
    }
    await manager.broadcast(json.dumps(message))

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
    
    # Don't auto-start data publishing - wait for client request
    publishing_running = False
    publishing_task = None
    logger.info("Server ready - waiting for client to start data processing")
    
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
    
    # Close all SSE connections
    for client_queue in manager.sse_clients[:]:
        try:
            await client_queue.put("__SHUTDOWN__")
        except Exception as e:
            logger.error(f"Error closing SSE connection: {e}")
    
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
        "timestamp": utc_timestamp()
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
        active_clients=manager.get_total_clients(),
        current_scenario=processor_status.get("current_scenario", current_scenario),
        last_heartbeat=utc_now()
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
        active_clients=manager.get_total_clients(),
        current_scenario=current_scenario,
        last_heartbeat=utc_now()
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
    # Check origin header for browser connections
    origin = websocket.headers.get('origin')
    allowed_origins = ServerConfig.CORS_ORIGINS
    
    if origin and origin not in allowed_origins:
        logger.warning(f"WebSocket connection rejected from origin: {origin}")
        await websocket.close(code=1008, reason="Origin not allowed")
        return
    
    await manager.connect(websocket)
    
    try:
        logger.info(f"WebSocket connection established from origin: {origin}")
        
        # Send initial connection message
        welcome_message = {
            "type": "connection",
            "data": {
                "message": "Connected to MarketDataPublisher",
                "timestamp": utc_timestamp(),
                "scenario": current_scenario
            }
        }
        await manager.send_personal_message(json.dumps(welcome_message), websocket)
        logger.info("Initial welcome message sent to client")
        
        # Keep connection alive and listen for client messages
        while True:
            try:
                # Use receive() instead of receive_text() to handle different message types
                message = await websocket.receive()
                
                if message['type'] == 'websocket.disconnect':
                    logger.info("Client requested disconnect")
                    break
                elif message['type'] == 'websocket.receive':
                    if 'text' in message:
                        data = message['text']
                        logger.debug(f"Received from client: {data}")
                        
                        # Echo back for now
                        response = {
                            "type": "echo",
                            "data": {
                                "message": f"Received: {data}",
                                "timestamp": utc_timestamp()
                            }
                        }
                        await manager.send_personal_message(json.dumps(response), websocket)
                
            except WebSocketDisconnect:
                logger.info("Client disconnected (WebSocketDisconnect)")
                break
            except Exception as e:
                logger.error(f"WebSocket error in message handling: {e}")
                break
                
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
    finally:
        manager.disconnect(websocket)

@app.get("/events")
async def sse_endpoint(request: Request):
    """Server-Sent Events endpoint for real-time data"""
    # Check origin header for browser connections
    origin = request.headers.get('origin')
    allowed_origins = ServerConfig.CORS_ORIGINS
    
    # if origin and origin not in allowed_origins:
    #     logger.warning(f"SSE connection rejected from origin: {origin}")
    #     raise HTTPException(status_code=403, detail="Origin not allowed")
    
    logger.info(f"SSE connection established from origin: {origin}")
    
    # Create a queue for this client
    client_queue = asyncio.Queue()
    manager.add_sse_client(client_queue)
    
    async def generate():
        try:
            # Send initial connection message
            welcome_message = {
                "type": "connection",
                "data": {
                    "message": "Connected to MarketDataPublisher via SSE",
                    "timestamp": utc_timestamp(),
                    "scenario": current_scenario
                }
            }
            yield f"data: {json.dumps(welcome_message)}\n\n"
            logger.info("Initial welcome message sent to SSE client")
            
            # Keep connection alive and send messages
            while True:
                try:
                    # Wait for message from queue (with timeout to send keepalive)
                    message = await asyncio.wait_for(client_queue.get(), timeout=15.0)
                    
                    if message == "__SHUTDOWN__":
                        logger.info("SSE client shutdown requested")
                        break
                    
                    # Send the message
                    yield f"data: {message}\n\n"
                    
                except asyncio.TimeoutError:
                    # Send keepalive message every 15 seconds
                    keepalive = {
                        "type": "keepalive",
                        "data": {"timestamp": utc_timestamp()}
                    }
                    yield f"data: {json.dumps(keepalive)}\n\n"
                
        except Exception as e:
            logger.error(f"SSE connection error: {e}")
        finally:
            # Clean up client
            manager.remove_sse_client(client_queue)
            logger.info("SSE client disconnected")
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": origin if origin in allowed_origins else "*",
            "Access-Control-Allow-Headers": "Cache-Control",
        }
    )

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
        "timestamp": utc_timestamp()
    }

# Global data publishing control
publishing_running = False
publishing_task = None

async def run_data_publishing():
    """Run the data publishing"""
    global data_publisher, queue_processor, publishing_running
    
    event_count = 0
    try:
        logger.info(f"Starting data publishing for scenario: {current_scenario}")
        async for orderbook in data_publisher.start_publishing(current_scenario, speed_multiplier=2.0, loop_continuously=True):
            if not publishing_running:
                logger.info("Data publishing stopped by request")
                break
            
            event_count += 1
            # Debug: Log every 100 events to see if publishing is working
            if event_count % 100 == 0:
                logger.info(f"📡 Publishing loop generated {event_count} events")
            
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
        "timestamp": utc_timestamp()
    }

@app.post("/start")
async def start_data_processing():
    """Start data processing and streaming"""
    global publishing_task, publishing_running
    
    if publishing_running and publishing_task and not publishing_task.done():
        return {
            "message": "Data processing is already running",
            "status": "running",
            "timestamp": utc_timestamp()
        }
    
    logger.info("Starting data processing requested by client")
    
    try:
        # Reset global counters and scenario to initial state
        global total_messages_processed, server_start_time, current_scenario
        total_messages_processed = 0
        server_start_time = time.time()
        current_scenario = ServerConfig.INITIAL_SCENARIO
        
        # Reset queue processor
        if queue_processor:
            await queue_processor.reset()
            queue_processor.switch_scenario(current_scenario)
            logger.info("Queue processor reset")
        
        # Reset data publisher
        if data_publisher:
            data_publisher.reset()
            data_publisher.data_loader.switch_scenario(current_scenario)
            logger.info("Data publisher reset")
        
        # Restart metrics collection
        metrics_collector.restart()
        
        # Start publishing task
        publishing_running = True
        publishing_task = asyncio.create_task(run_data_publishing())
        logger.info("Data processing started")
        
        return {
            "message": "Data processing started successfully",
            "status": "running",
            "timestamp": utc_timestamp()
        }
        
    except Exception as e:
        logger.error(f"Error starting data processing: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to start data processing: {str(e)}"}
        )

@app.post("/stop")
async def stop_data_processing():
    """Stop data processing and streaming"""
    global publishing_task, publishing_running
    
    if not publishing_running:
        return {
            "message": "Data processing is already stopped",
            "status": "stopped",
            "timestamp": utc_timestamp()
        }
    
    logger.info("Stopping data processing requested by client")
    
    try:
        # Stop current publishing task
        publishing_running = False
        if publishing_task and not publishing_task.done():
            publishing_task.cancel()
            logger.info("Cancelled publishing task")
            
            # Wait for cancellation to complete
            try:
                await asyncio.wait_for(publishing_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.info("Publishing task cancellation completed")
        
        # Stop metrics collection
        metrics_collector.stop()
        
        logger.info("Data processing stopped")
        
        return {
            "message": "Data processing stopped successfully",
            "status": "stopped",
            "timestamp": utc_timestamp()
        }
        
    except Exception as e:
        logger.error(f"Error stopping data processing: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to stop data processing: {str(e)}"}
        )

@app.get("/status/processing")
async def get_processing_status():
    """Get current data processing status"""
    global publishing_running, publishing_task
    
    is_running = publishing_running and publishing_task and not publishing_task.done()
    
    return {
        "status": "running" if is_running else "stopped",
        "publishing_running": publishing_running,
        "task_active": publishing_task is not None and not publishing_task.done() if publishing_task else False,
        "timestamp": utc_timestamp()
    }



if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=ServerConfig.HOST,
        port=ServerConfig.PORT,
        reload=ServerConfig.DEBUG,
        log_level=ServerConfig.LOG_LEVEL.lower()
    ) 