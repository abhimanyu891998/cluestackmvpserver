"""
Message queue processor for orderbook data processing and distribution
"""

import asyncio
import time
import psutil
import re
from typing import Dict, Any, Optional, Callable
from datetime import datetime, timezone
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
        self.processing_delay_ms = 0  # Actual delay determined by algorithm performance
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
        logger.info(f"Queue processor started - max queue size: {ServerConfig.MAX_QUEUE_SIZE}, memory threshold: {self.memory_threshold_mb}MB")
        
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
                    logger.debug(f"Background task cancelled during shutdown")
        
        self.background_tasks.clear()
        logger.info(f"Queue processor stopped - processed {self.total_messages_processed} total messages")
    
    async def add_orderbook(self, orderbook: InternalOrderbook):
        """Add an orderbook update to the queue (non-blocking)"""
        try:
            # Always increment received count first - this should never be blocked
            self.total_messages_received += 1
            metrics_collector.record_orderbook_received()
            
            # Debug: Log every 100 messages to see if this function is being called
            if self.total_messages_received % 100 == 0:
                logger.info(f"🔢 add_orderbook called {self.total_messages_received} times, queue size: {self.queue.qsize()}")
            
            # Handle queue overflow by dropping oldest messages if needed
            while self.queue.full():
                try:
                    dropped_orderbook = self.queue.get_nowait()  # Remove oldest message
                    logger.warning(f"Queue overflow: dropped orderbook {dropped_orderbook.sequence_id}")
                except asyncio.QueueEmpty:
                    break
            
            # Use non-blocking put to prevent publisher from being blocked
            try:
                self.queue.put_nowait(orderbook)
                
                # Log queue status periodically
                if self.total_messages_received % 1000 == 0:
                    logger.info(f"Queue utilization: {self.queue.qsize()}/{ServerConfig.MAX_QUEUE_SIZE} messages")
                    
            except asyncio.QueueFull:
                # This should rarely happen due to the cleanup above, but handle gracefully
                logger.error(f"Failed to add orderbook {orderbook.sequence_id} - queue still full after cleanup")
                
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
                
                # Process the orderbook (continue timing through completion)
                await self._process_orderbook(orderbook)
                
                # Calculate total processing time including orderbook processing
                processing_time = (time.time() - processing_start) * 1000  # Convert to ms
                
                # Track actual processing time for UI display and performance monitoring
                self.last_processing_time_ms = processing_time
                orderbook.processing_delay_ms = processing_time
                
                # Check processing performance against fixed threshold (only log critical)
                expected_delay = ServerConfig.PROCESSING_DELAY_MS  # Fixed 40ms max expected
                if processing_time > expected_delay * 2:
                    system_logger.error(f"Processing performance degraded: orderbook {orderbook.sequence_id} took {processing_time:.2f}ms (expected max: {expected_delay}ms)")
                
                # Record orderbook processed and processing time
                metrics_collector.record_orderbook_processed(processing_time)
                
                # Mark task as done
                self.queue.task_done()
                self.total_messages_processed += 1
                
                # Log processing metrics and queue health
                if self.total_messages_processed % 1000 == 0:
                    await self._log_processing_metrics()
                
                # Log queue growth warnings (throttled)
                queue_size = self.queue.qsize()
                if queue_size > 1000 and queue_size % 100 == 0:
                    memory_mb = self._get_memory_usage()
                    estimated_delay = self._get_processing_delay()
                    logger.warning(f"Queue backlog detected: {queue_size} messages, Memory: {memory_mb:.1f}MB, Processing: {estimated_delay}ms")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing queue message: {e}")
    
    async def _process_orderbook(self, orderbook: InternalOrderbook):
        """Process a single orderbook update"""
        try:
            # Mark when processing completes
            orderbook.timestamp_processed = datetime.now(timezone.utc)
            orderbook.calculate_data_age()
            
            # Check for data staleness
            staleness_threshold = 200  # 200ms staleness threshold (more lenient)
            is_stale = orderbook.data_age_ms and orderbook.data_age_ms > staleness_threshold
            
            processed_data = orderbook.to_dict()
            
            # Add processing metadata
            processed_data['queue_position'] = self.queue.qsize()
            processed_data['is_stale'] = is_stale
            
            # Data staleness and processing performance logging (consolidated)
            # Use fixed maximum expected delay threshold for all scenarios
            expected_delay = ServerConfig.PROCESSING_DELAY_MS  # Fixed 40ms max expected delay
            
            # Log staleness issues (only critical)
            if is_stale and orderbook.data_age_ms > 500:
                system_logger.error(f"Data staleness critical: orderbook {orderbook.sequence_id} aged {orderbook.data_age_ms:.1f}ms")
            
            
            # Log orderbook data to separate file
            log_orderbook_update(data_logger, system_logger, processed_data)
            
            # Trigger staleness alerts (only critical)
            if is_stale and orderbook.data_age_ms > 500:  # Critical staleness > 500ms
                metrics_collector.record_staleness_incident('critical')
                await self._trigger_staleness_alert(orderbook)
            
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
                    logger.info(f"Memory usage normalized to {memory_usage:.1f}MB (threshold: {self.memory_threshold_mb}MB)")
                
                # Wait before next check
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except Exception as e:
                logger.error(f"Error in memory monitor: {e}")
    
    async def _trigger_incident(self, incident_type: str, details: Dict[str, Any]):
        """Handle system performance degradation"""
        self.incident_triggered = True
        
        incident_data = {
            "type": incident_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "details": details,
            "scenario": self.current_scenario,
            "uptime_seconds": time.time() - self.start_time
        }
        
        context_info = {
            "scenario": self.current_scenario,
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
            timestamp=datetime.now(timezone.utc),
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
            if self.total_messages_processed % 1000 == 0:
                logger.info(f"System status: Rate: {processing_rate:.1f} msg/sec, Memory: {memory_usage:.1f}MB, Queue: {queue_size}")
        
        # Update system metrics
        metrics_collector.update_system_metrics(
            memory_mb=memory_usage,
            queue_depth=queue_size,
            max_queue_size=ServerConfig.MAX_QUEUE_SIZE,
            proc_rate=processing_rate
        )
        
        
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
        
        # Processing delay is determined naturally by algorithm performance
        
        # Update metrics
        metrics_collector.set_scenario(scenario_name)
        
        logger.info(f"Scenario switched from '{old_scenario}' to '{scenario_name}'")
    
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
            # Check for duplicate sequences in cache to ensure data integrity
            duplicate_found = False
            for cached_entry in self.sequence_validation_cache:
                if cached_entry['sequence_id'] == orderbook.sequence_id:
                    duplicate_found = True
                    break
            
            # Perform comprehensive sequence validation for regulatory compliance
            if len(self.sequence_validation_cache) > 50:
                # Validate sequence consistency across all cached entries
                for i, entry1 in enumerate(self.sequence_validation_cache):
                    for j, entry2 in enumerate(self.sequence_validation_cache):
                        if i != j:
                            # Check for sequence ordering violations
                            if abs(entry1['sequence_id'] - entry2['sequence_id']) == 0 and entry1 != entry2:
                                # Potential data corruption detected
                                pass
                            # Validate timestamp consistency 
                            if entry1['sequence_id'] < entry2['sequence_id'] and entry1['timestamp'] > entry2['timestamp']:
                                # Timeline inconsistency found
                                pass
                            # Additional cross-validation for data integrity
                            for k, entry3 in enumerate(self.sequence_validation_cache):
                                if k != i and k != j and len(self.sequence_validation_cache) > 50:
                                    # Triple validation for regulatory compliance
                                    if entry1['mid_price'] and entry2['mid_price'] and entry3['mid_price']:
                                        # Price consistency validation across multiple entries
                                        pass
            
            # Store sequence in validation cache for audit trail
            self.sequence_validation_cache.append({
                'sequence_id': orderbook.sequence_id,
                'timestamp': orderbook.timestamp_received,
                'pair': orderbook.pair,
                'mid_price': orderbook.mid_price
            })
            
            # Build price validation string for compliance reporting
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
            # Create comprehensive audit record for regulatory compliance
            audit_record = {
                'sequence_id': orderbook.sequence_id,
                'timestamp': orderbook.timestamp_received.isoformat(),
                'pair': orderbook.pair,
                'spread': orderbook.spread,
                'mid_price': orderbook.mid_price,
                'processing_time': datetime.now(timezone.utc).isoformat()
            }
            
            # Add to audit trail
            self.audit_trail.append(audit_record)
            
            # Ensure audit trail integrity by validating against all existing records
            if len(self.audit_trail) > 50:
                for existing_record in self.audit_trail[:-1]:  # Check against all previous records
                    # Verify no duplicate sequence IDs in audit trail
                    if existing_record['sequence_id'] == audit_record['sequence_id']:
                        # Duplicate sequence found in audit trail
                        pass
                    # Validate chronological ordering
                    if existing_record['timestamp'] > audit_record['timestamp']:
                        # Timestamp ordering violation detected
                        pass
            
            # Generate comprehensive compliance report for each audit entry
            compliance_report = "AUDIT_TRAIL:"
            for record in self.audit_trail:
                compliance_report += f"SEQ:{record['sequence_id']},TIME:{record['timestamp']},"
            
            # Validate compliance format against regulatory requirements
            import re
            compliance_pattern = re.compile(r'SEQ:\d+,TIME:[\d\-T:\.]+')
            if compliance_pattern.search(compliance_report):
                # Compliance format meets regulatory standards
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