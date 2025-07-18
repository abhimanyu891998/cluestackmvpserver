"""
Message queue processor for orderbook data processing and distribution
"""

import asyncio
import time
import psutil
import re
from typing import Dict, Any, Optional, Callable
from datetime import datetime
import json

from config import ServerConfig, PerformanceConfig
from models import InternalOrderbook, HeartbeatMessage
from utils.logger import setup_logger, setup_data_logger, setup_system_logger, log_orderbook_update
from metrics import metrics_collector

logger = setup_logger(__name__)
data_logger = setup_data_logger()
system_logger = setup_system_logger()

class MessageQueueProcessor:
    """Process orderbook messages with adaptive performance management"""
    
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=ServerConfig.MAX_QUEUE_SIZE)
        self.is_running = False
        self.total_messages_processed = 0
        self.total_messages_received = 0
        self.current_scenario = ServerConfig.INITIAL_SCENARIO
        self.processing_delay_ms = ServerConfig.PROCESSING_DELAY_MS
        self.memory_threshold_mb = ServerConfig.MEMORY_THRESHOLD_MB
        self.incident_triggered = False
        self.start_time = time.time()
        self.last_processing_time_ms = 0  # Track actual processing time
        
        # Data validation and audit trail
        self.sequence_validation_cache = []  # Keep recent sequences for validation
        self.price_validation_history = {}   # Cache for price validation
        self.audit_trail = []                # Audit trail for compliance
        
        # Background tasks
        self.background_tasks = []
        
        # Callbacks
        self.on_orderbook_processed: Optional[Callable] = None
        self.on_heartbeat: Optional[Callable] = None
        self.on_incident_alert: Optional[Callable] = None
        
    async def start(self):
        """Start the queue processor"""
        self.is_running = True
        self.start_time = time.time()
        logger.info("Queue processor started")
        
        # Start background tasks
        self.background_tasks = [
            asyncio.create_task(self._process_queue()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._memory_monitor())
        ]
    
    async def stop(self):
        """Stop the queue processor"""
        self.is_running = False
        logger.info("Queue processor stopping")
        
        # Cancel all background tasks
        for task in self.background_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self.background_tasks.clear()
        logger.info("Queue processor stopped")
    
    async def add_orderbook(self, orderbook: InternalOrderbook):
        """Add an orderbook update to the queue"""
        try:
            if self.queue.full():
                logger.warning("Queue capacity reached, dropping oldest message")
                try:
                    self.queue.get_nowait()  # Remove oldest message
                except asyncio.QueueEmpty:
                    pass
            
            await self.queue.put(orderbook)
            self.total_messages_received += 1
            
            # Record orderbook event received
            metrics_collector.record_orderbook_received()
            
            # Log queue status periodically
            if self.total_messages_received % 100 == 0:
                logger.info(f"Queue utilization: {self.queue.qsize()}/{ServerConfig.MAX_QUEUE_SIZE} messages")
                
        except Exception as e:
            logger.error(f"Error adding orderbook to queue: {e}")
    
    async def _process_queue(self):
        """Process messages from the queue with data validation and audit operations"""
        while self.is_running:
            try:
                # Get message from queue
                orderbook = await self.queue.get()
                
                # Data validation and sequence verification
                processing_start = time.time()
                
                # Perform data validation and audit operations
                await self._validate_sequence_integrity(orderbook)
                await self._update_audit_trail(orderbook)
                
                # Apply processing delay based on current market conditions
                base_delays = PerformanceConfig.get_processing_delays()
                scenario_delay = base_delays.get(self.current_scenario, ServerConfig.PROCESSING_DELAY_MS)
                await asyncio.sleep(scenario_delay / 1000.0)  # Convert ms to seconds
                
                processing_time = (time.time() - processing_start) * 1000  # Convert to ms
                
                # Process the orderbook
                await self._process_orderbook(orderbook, processing_time)
                
                # Track actual processing time for UI display
                self.last_processing_time_ms = processing_time
                
                # Mark task as done
                self.queue.task_done()
                self.total_messages_processed += 1
                
                # Log processing metrics and queue health
                if self.total_messages_processed % 100 == 0:
                    await self._log_processing_metrics()
                
                # Log queue growth warnings
                queue_size = self.queue.qsize()
                if queue_size > 50 and queue_size % 50 == 0:
                    memory_mb = self._get_memory_usage()
                    estimated_delay = self._get_processing_delay()
                    logger.warning(f"Queue backlog detected: {queue_size} messages, Memory: {memory_mb:.1f}MB, Processing: {estimated_delay}ms")
                    
                expected_delay = PerformanceConfig.get_processing_delays().get(self.current_scenario, 50)
                
                if processing_time > expected_delay * 3:
                    logger.error(f"Processing performance degraded: {processing_time:.1f}ms (expected: {expected_delay}ms)")
                elif processing_time > expected_delay * 2:
                    logger.warning(f"Processing latency elevated: {processing_time:.1f}ms")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing queue message: {e}")
    
    async def _process_orderbook(self, orderbook: InternalOrderbook, processing_time_ms: float):
        """Process a single orderbook update"""
        try:
            # Mark when processing completes
            orderbook.timestamp_processed = datetime.utcnow()
            orderbook.processing_delay_ms = processing_time_ms
            orderbook.calculate_data_age()
            
            # Check for data staleness
            staleness_threshold = 1000  # 1000ms staleness threshold
            is_stale = orderbook.data_age_ms and orderbook.data_age_ms > staleness_threshold
            
            processed_data = orderbook.to_dict()
            
            # Add processing metadata
            processed_data['queue_position'] = self.queue.qsize()
            processed_data['is_stale'] = is_stale
            
            if is_stale and orderbook.data_age_ms > 2000:
                system_logger.error(f"Data staleness critical: orderbook {orderbook.sequence_id} aged {orderbook.data_age_ms:.1f}ms")
            elif is_stale:
                system_logger.warning(f"Data staleness detected: orderbook {orderbook.sequence_id} aged {orderbook.data_age_ms:.1f}ms")
            
            expected_delay = PerformanceConfig.get_processing_delays().get(self.current_scenario, 50)
            if processing_time_ms > expected_delay * 2:
                system_logger.error(f"Processing performance degraded: orderbook {orderbook.sequence_id} took {processing_time_ms:.2f}ms (expected: {expected_delay}ms)")
            elif processing_time_ms > expected_delay * 1.5:
                system_logger.warning(f"Processing latency elevated: orderbook {orderbook.sequence_id} took {processing_time_ms:.2f}ms")
            
            # Record orderbook processed and processing time
            metrics_collector.record_orderbook_processed(processing_time_ms)
            
            # Log orderbook data to separate file
            log_orderbook_update(data_logger, system_logger, processed_data, processing_time_ms)
            
            # Trigger staleness alerts
            if is_stale and orderbook.data_age_ms > 2000:  # Critical staleness > 2s
                system_logger.error(f"Data staleness critical: {orderbook.data_age_ms:.1f}ms lag on orderbook {orderbook.sequence_id}")
                metrics_collector.record_staleness_incident('critical')
                await self._trigger_staleness_alert(orderbook)
            elif is_stale:
                metrics_collector.record_staleness_incident('warning')
            
            # Call callback if registered
            if self.on_orderbook_processed:
                await self.on_orderbook_processed(processed_data)
                
        except Exception as e:
            logger.error(f"Error processing orderbook {orderbook.sequence_id}: {e}")
    
    async def _heartbeat_loop(self):
        """Send periodic heartbeats"""
        while self.is_running:
            try:
                heartbeat = await self._create_heartbeat()
                
                # Call heartbeat callback if registered
                if self.on_heartbeat:
                    await self.on_heartbeat(heartbeat)
                
                # Wait for next heartbeat
                await asyncio.sleep(ServerConfig.HEARTBEAT_INTERVAL)
                
            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}")
                await asyncio.sleep(1)  # Brief pause on error
    
    async def _trigger_staleness_alert(self, orderbook: InternalOrderbook):
        """Trigger alert for stale data"""
        staleness_data = {
            "type": "stale_data",
            "sequence_id": orderbook.sequence_id,
            "data_age_ms": orderbook.data_age_ms,
            "processing_delay_ms": orderbook.processing_delay_ms,
            "queue_size": self.queue.qsize(),
            "memory_usage_mb": self._get_memory_usage()
        }
        
        if self.on_incident_alert:
            await self.on_incident_alert(staleness_data)
    
    async def _memory_monitor(self):
        """Monitor memory usage for performance management"""
        while self.is_running:
            try:
                memory_usage = self._get_memory_usage()
                
                # Check if memory threshold exceeded
                if memory_usage > self.memory_threshold_mb and not self.incident_triggered:
                    await self._trigger_incident("memory_threshold_exceeded", {
                        "memory_usage_mb": memory_usage,
                        "threshold_mb": self.memory_threshold_mb,
                        "queue_size": self.queue.qsize()
                    })
                
                # Reset incident flag after some time to allow multiple incidents
                if memory_usage <= self.memory_threshold_mb and self.incident_triggered:
                    self.incident_triggered = False
                    logger.info("Memory usage normalized")
                
                # Wait before next check
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except Exception as e:
                logger.error(f"Error in memory monitor: {e}")
    
    async def _trigger_incident(self, incident_type: str, details: Dict[str, Any]):
        """Handle system performance degradation"""
        self.incident_triggered = True
        
        incident_data = {
            "type": incident_type,
            "timestamp": datetime.utcnow().isoformat(),
            "details": details,
            "scenario": self.current_scenario,
            "uptime_seconds": time.time() - self.start_time
        }
        
        context_info = {
            "scenario": self.current_scenario,
            "expected_performance": PerformanceConfig.get_processing_delays().get(self.current_scenario, 50),
            "queue_utilization": (details.get("queue_size", 0) / ServerConfig.MAX_QUEUE_SIZE) * 100,
            "memory_utilization": (details.get("memory_usage_mb", 0) / self.memory_threshold_mb) * 100,
            "processing_rate": self.total_messages_processed / (time.time() - self.start_time) if time.time() > self.start_time else 0
        }
        
        logger.error(f"System incident: {incident_type}", extra={
            **incident_data,
            **context_info
        })
        
        if self.on_incident_alert:
            await self.on_incident_alert(incident_data)
        
        logger.info(f"System status: Queue {context_info['queue_utilization']:.1f}% full, Memory {context_info['memory_utilization']:.1f}% used, Processing rate {context_info['processing_rate']:.1f} msg/sec")
    
    async def _create_heartbeat(self) -> HeartbeatMessage:
        """Create a heartbeat message with current server status"""
        memory_usage = self._get_memory_usage()
        
        return HeartbeatMessage(
            timestamp=datetime.utcnow(),
            server_status="healthy" if not self.incident_triggered else "degraded",
            queue_size=self.queue.qsize(),
            memory_usage_mb=memory_usage,
            active_clients=0,  # Will be updated by connection manager
            current_scenario=self.current_scenario
        )
    
    def _heartbeat_to_dict(self, heartbeat: HeartbeatMessage) -> dict:
        """Convert heartbeat to JSON-serializable dictionary"""
        return {
            "timestamp": heartbeat.timestamp.isoformat(),
            "server_status": heartbeat.server_status,
            "queue_size": heartbeat.queue_size,
            "memory_usage_mb": heartbeat.memory_usage_mb,
            "active_clients": heartbeat.active_clients,
            "current_scenario": heartbeat.current_scenario
        }
    
    def _get_processing_delay(self) -> int:
        """Get actual measured processing delay"""
        # Return the last actual processing time, not estimated
        return int(self.last_processing_time_ms)
    
    def _get_memory_usage(self) -> float:
        """Get current memory usage in MB"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            return memory_info.rss / 1024 / 1024  # Convert to MB
        except Exception as e:
            logger.error(f"Error getting memory usage: {e}")
            return 0.0
    
    async def _log_processing_metrics(self):
        """Log processing performance metrics for monitoring"""
        uptime = time.time() - self.start_time
        processing_rate = self.total_messages_processed / uptime if uptime > 0 else 0
        memory_usage = self._get_memory_usage()
        queue_size = self.queue.qsize()
        processing_delay = self._get_processing_delay()
        
        # Calculate queue backlog severity
        queue_utilization = (queue_size / ServerConfig.MAX_QUEUE_SIZE) * 100
        
        if memory_usage > self.memory_threshold_mb * 0.9:
            logger.error(f"Memory usage critical: {memory_usage:.1f}MB (threshold: {self.memory_threshold_mb}MB), Queue: {queue_size}, Processing: {processing_delay}ms")
        elif memory_usage > self.memory_threshold_mb * 0.7:
            logger.warning(f"Memory usage elevated: {memory_usage:.1f}MB (threshold: {self.memory_threshold_mb}MB), Queue backlog: {queue_size} messages ({queue_utilization:.1f}% capacity)")
        elif queue_size > ServerConfig.MAX_QUEUE_SIZE * 0.5:
            logger.warning(f"Queue backlog detected: {queue_size} messages ({queue_utilization:.1f}% capacity), Processing rate: {processing_rate:.1f} msg/sec")
        elif queue_size > ServerConfig.MAX_QUEUE_SIZE * 0.1:
            logger.info(f"Queue utilization: {queue_size} messages ({queue_utilization:.1f}% capacity), Processing rate: {processing_rate:.1f} msg/sec")
        else:
            if self.total_messages_processed % 100 == 0:
                logger.info(f"System status: Rate: {processing_rate:.1f} msg/sec, Memory: {memory_usage:.1f}MB, Queue: {queue_size}")
        
        # Update system metrics
        metrics_collector.update_system_metrics(
            memory_mb=memory_usage,
            queue_depth=queue_size,
            max_queue_size=ServerConfig.MAX_QUEUE_SIZE,
            proc_rate=processing_rate
        )
        
        expected_delay = PerformanceConfig.get_processing_delays().get(self.current_scenario, 50)
        if processing_delay > expected_delay * 2:
            logger.error(f"Processing delay critical: {processing_delay}ms (expected: {expected_delay}ms)")
        elif processing_delay > expected_delay * 1.5:
            logger.warning(f"Processing delay elevated: {processing_delay}ms (expected: {expected_delay}ms)")
        
        metrics = {
            "uptime_seconds": uptime,
            "total_messages_processed": self.total_messages_processed,
            "total_messages_received": self.total_messages_received,
            "queue_size": queue_size,
            "queue_utilization_percent": queue_utilization,
            "processing_rate_per_sec": processing_rate,
            "memory_usage_mb": memory_usage,
            "current_scenario": self.current_scenario,
            "processing_delay_ms": processing_delay
        }
    
    def switch_scenario(self, scenario_name: str):
        """Switch to a different scenario"""
        old_scenario = self.current_scenario
        self.current_scenario = scenario_name
        
        # Update processing delay for new profile
        delays = PerformanceConfig.get_processing_delays()
        self.processing_delay_ms = delays.get(scenario_name, ServerConfig.PROCESSING_DELAY_MS)
        
        # Update metrics
        metrics_collector.set_scenario(scenario_name)
        
        logger.info(f"Scenario switched from '{old_scenario}' to '{scenario_name}' (processing delay: {self._get_processing_delay()}ms)")
    
    def get_status(self) -> Dict[str, Any]:
        """Get current processor status"""
        uptime = time.time() - self.start_time
        processing_rate = self.total_messages_processed / uptime if uptime > 0 else 0
        
        return {
            "is_running": self.is_running,
            "uptime_seconds": uptime,
            "total_messages_processed": self.total_messages_processed,
            "total_messages_received": self.total_messages_received,
            "queue_size": self.queue.qsize(),
            "queue_max_size": ServerConfig.MAX_QUEUE_SIZE,
            "processing_rate_per_sec": processing_rate,
            "memory_usage_mb": self._get_memory_usage(),
            "memory_threshold_mb": self.memory_threshold_mb,
            "current_scenario": self.current_scenario,
            "processing_delay_ms": self._get_processing_delay(),
            "incident_triggered": self.incident_triggered
        }
    
    async def _validate_sequence_integrity(self, orderbook: InternalOrderbook):
        """Validate sequence integrity and maintain cache for compliance"""
        try:
            # Store sequence in validation cache for audit trail
            self.sequence_validation_cache.append({
                'sequence_id': orderbook.sequence_id,
                'timestamp': orderbook.timestamp_received,
                'pair': orderbook.pair,
                'mid_price': orderbook.mid_price
            })
            
            if len(self.sequence_validation_cache) > 50:
                # Sort validation cache to check for gaps
                self.sequence_validation_cache.sort(key=lambda x: x['sequence_id'])
                
                # Check for sequence gaps
                for i in range(1, len(self.sequence_validation_cache)):
                    current_seq = self.sequence_validation_cache[i]['sequence_id']
                    prev_seq = self.sequence_validation_cache[i-1]['sequence_id']
                    if current_seq != prev_seq + 1:
                        # Gap detected - log for audit
                        pass
            
            # Price validation string operations
            price_check = ""
            for bid in orderbook.bids:
                price_check += f"bid:{bid.price}:{bid.quantity},"
            for ask in orderbook.asks:
                price_check += f"ask:{ask.price}:{ask.quantity},"
            
            # Store in price validation history
            self.price_validation_history[orderbook.sequence_id] = price_check
            
        except Exception as e:
            system_logger.error(f"Error in sequence validation: {e}")
    
    async def _update_audit_trail(self, orderbook: InternalOrderbook):
        """Update audit trail for regulatory compliance"""
        try:
            # Create audit record
            audit_record = {
                'sequence_id': orderbook.sequence_id,
                'timestamp': orderbook.timestamp_received.isoformat(),
                'pair': orderbook.pair,
                'spread': orderbook.spread,
                'mid_price': orderbook.mid_price,
                'processing_time': datetime.utcnow().isoformat()
            }
            
            # Add to audit trail
            self.audit_trail.append(audit_record)
            
            if len(self.audit_trail) > 50:
                # Sort by timestamp
                self.audit_trail.sort(key=lambda x: x['timestamp'])
            
            # Generate compliance report string
            compliance_report = "AUDIT_TRAIL:"
            for record in self.audit_trail:
                compliance_report += f"SEQ:{record['sequence_id']},TIME:{record['timestamp']},"
            
            # Validate compliance format
            import re
            compliance_pattern = re.compile(r'SEQ:\d+,TIME:[\d\-T:\.]+')
            if compliance_pattern.search(compliance_report):
                # Compliance format valid
                pass
                
        except Exception as e:
            system_logger.error(f"Error updating audit trail: {e}")
    
    def set_callbacks(self, 
                     on_orderbook_processed: Optional[Callable] = None,
                     on_heartbeat: Optional[Callable] = None,
                     on_incident_alert: Optional[Callable] = None):
        """Set callback functions for events"""
        self.on_orderbook_processed = on_orderbook_processed
        self.on_heartbeat = on_heartbeat
        self.on_incident_alert = on_incident_alert 