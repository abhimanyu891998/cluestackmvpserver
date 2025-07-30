"""
Prometheus metrics collection for MarketDataPublisher
"""

import time
import requests
import threading
import snappy
import struct
from typing import Dict, Any, List
from prometheus_client import Counter, Gauge, Histogram, CollectorRegistry, generate_latest
from config import ServerConfig

# Create custom registry
registry = CollectorRegistry()

# System metrics
memory_usage = Gauge(
    'marketdata_memory_usage_bytes',
    'Current memory usage in bytes',
    registry=registry
)

queue_size = Gauge(
    'marketdata_queue_size',
    'Current queue size',
    registry=registry
)

queue_utilization = Gauge(
    'marketdata_queue_utilization_percent',
    'Queue utilization percentage',
    registry=registry
)

processing_rate = Gauge(
    'marketdata_processing_rate_per_second',
    'Message processing rate per second',
    registry=registry
)

# Orderbook-specific metrics
orderbook_events_received = Counter(
    'marketdata_orderbook_events_received_total',
    'Total number of orderbook events received',
    registry=registry
)

orderbook_events_received_rate = Gauge(
    'marketdata_orderbook_events_received_rate_per_second',
    'Current rate of orderbook events received per second',
    registry=registry
)

orderbook_events_processed = Counter(
    'marketdata_orderbook_events_processed_total',
    'Total number of orderbook events processed',
    registry=registry
)

orderbook_processing_duration = Histogram(
    'marketdata_orderbook_processing_duration_seconds',
    'Time taken to process orderbook events',
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0),
    registry=registry
)

staleness_incidents = Counter(
    'marketdata_staleness_incidents_total',
    'Total number of data staleness incidents',
    ['severity'],
    registry=registry
)

# Scenario tracking
current_scenario = Gauge(
    'marketdata_current_scenario',
    'Current market scenario',
    ['scenario_name'],
    registry=registry
)

class MetricsCollector:
    """Collect and update metrics for MarketDataPublisher"""
    
    def __init__(self):
        self.start_time = time.time()
        self.last_update = time.time()
        self.remote_write_enabled = ServerConfig.ENABLE_PROMETHEUS_REMOTE_WRITE
        self.remote_write_url = ServerConfig.PROMETHEUS_URL
        self.remote_write_auth = (ServerConfig.PROMETHEUS_USERNAME, ServerConfig.PROMETHEUS_PASSWORD)
        
        # Rate calculation tracking
        self.last_events_count = 0
        self.last_rate_update = time.time()
        
        # Shutdown control
        self.shutdown_flag = threading.Event()
        
        # Start background thread for remote write
        if self.remote_write_enabled:
            self.remote_write_thread = threading.Thread(target=self._remote_write_loop, daemon=True)
            self.remote_write_thread.start()
            
        # Start background thread for rate calculation
        self.rate_thread = threading.Thread(target=self._rate_calculation_loop, daemon=True)
        self.rate_thread.start()
        
    def update_system_metrics(self, memory_mb: float, queue_depth: int, max_queue_size: int, proc_rate: float):
        """Update system-level metrics"""
        memory_usage.set(memory_mb * 1024 * 1024)  # Convert MB to bytes
        queue_size.set(queue_depth)
        queue_utilization.set((queue_depth / max_queue_size) * 100)
        processing_rate.set(proc_rate)
        
    def record_orderbook_received(self):
        """Record an orderbook event received"""
        orderbook_events_received.inc()
        
    def record_orderbook_processed(self, processing_time_ms: float):
        """Record an orderbook event processed"""
        orderbook_events_processed.inc()
        orderbook_processing_duration.observe(processing_time_ms / 1000.0)  # Convert to seconds
        
    def record_staleness_incident(self, severity: str):
        """Record a data staleness incident"""
        staleness_incidents.labels(severity=severity).inc()
        
    def set_scenario(self, scenario_name: str):
        """Set current scenario"""
        # Reset all scenario labels to 0
        for scenario in ['stable-mode', 'burst-mode']:
            current_scenario.labels(scenario_name=scenario).set(0)
        # Set current scenario to 1
        current_scenario.labels(scenario_name=scenario_name).set(1)
        
    def get_metrics(self) -> bytes:
        """Get metrics in Prometheus format"""
        return generate_latest(registry)
    
    def _rate_calculation_loop(self):
        """Background thread to calculate events received rate"""
        while not self.shutdown_flag.is_set():
            try:
                current_time = time.time()
                current_events = orderbook_events_received._value.get()
                
                # Calculate time elapsed since last update (minimum 1 second)
                time_elapsed = max(current_time - self.last_rate_update, 1.0)
                
                # Calculate events since last update
                events_delta = current_events - self.last_events_count
                
                # Calculate rate (events per second)
                rate = events_delta / time_elapsed
                
                # Update the rate gauge
                orderbook_events_received_rate.set(rate)
                
                # Update tracking variables
                self.last_events_count = current_events
                self.last_rate_update = current_time
                
                if self.shutdown_flag.wait(2):  # Update rate every 2 seconds, check shutdown
                    break
                
            except Exception as e:
                print(f"Error in rate calculation: {e}")
                if self.shutdown_flag.wait(5):  # Wait 5 seconds on error, check shutdown
                    break
                
    def _remote_write_loop(self):
        """Background thread to push metrics to Prometheus remote write endpoint"""
        while not self.shutdown_flag.is_set():
            try:
                if self.remote_write_enabled:
                    self._push_metrics_to_remote()
                if self.shutdown_flag.wait(5):  # Push metrics every 5 seconds, check shutdown
                    break
            except Exception as e:
                print(f"Error in remote write: {e}")
                if self.shutdown_flag.wait(30):  # Wait longer on error, check shutdown
                    break
    
    def _push_metrics_to_remote(self):
        """Push metrics to Prometheus remote write endpoint"""
        try:
            # Collect current metric values for memory and events only
            timestamp_ms = int(time.time() * 1000)
            
            # Prepare metrics data
            metrics_to_push = []
            
            # Get current memory usage directly using psutil
            import psutil
            try:
                process = psutil.Process()
                memory_info = process.memory_info()
                current_memory_bytes = memory_info.rss  # Get current memory in bytes
                memory_mb = current_memory_bytes / (1024 * 1024)
                
                # Update the memory gauge with current value
                memory_usage.set(current_memory_bytes)
                
                # Always push memory metrics (remove the > 0 check)
                metrics_to_push.append(f"marketdata_memory_usage_bytes {current_memory_bytes} {timestamp_ms}")
                
            except Exception as e:
                print(f"Error getting current memory usage: {e}")
                # Fallback to gauge value if psutil fails
                memory_bytes = memory_usage._value.get()
                memory_mb = memory_bytes / (1024*1024)
                if memory_bytes > 0:
                    metrics_to_push.append(f"marketdata_memory_usage_bytes {memory_bytes} {timestamp_ms}")
            
            # Orderbook events received (incoming events)
            events_received = orderbook_events_received._value.get()
            if events_received > 0:
                metrics_to_push.append(f"marketdata_orderbook_events_received_total {events_received} {timestamp_ms}")
            
            # Orderbook events received rate (for hill visualization)
            events_rate = orderbook_events_received_rate._value.get()
            if events_rate >= 0:  # Include 0 rates to show valleys
                metrics_to_push.append(f"marketdata_orderbook_events_received_rate_per_second {events_rate} {timestamp_ms}")
            
            if metrics_to_push:
                # Send to Grafana Cloud
                success = self._send_to_grafana_cloud(metrics_to_push)
                
                if success:
                    print("📊 Metrics pushed to Grafana Cloud successfully")
                    print(f"   - Memory: {memory_mb:.1f}MB")
                    print(f"   - Events Received: {events_received}")
                    events_rate = orderbook_events_received_rate._value.get()
                    print(f"   - Events Rate: {events_rate:.1f}/sec")
                else:
                    print("⚠️  Failed to push metrics to Grafana Cloud")
            
        except Exception as e:
            print(f"Error pushing metrics to remote: {e}")
    
    def _send_to_grafana_cloud(self, metrics_data: List[str]) -> bool:
        """Send metrics to Grafana Cloud using proper protobuf"""
        try:
            # Create protobuf WriteRequest message manually
            import io
            
            # Extract metric values
            memory_value = None
            events_value = None
            events_rate_value = None
            timestamp_ms = int(time.time() * 1000)
            
            for metric in metrics_data:
                parts = metric.split()
                if len(parts) >= 2:
                    if 'memory_usage_bytes' in metric:
                        memory_value = float(parts[1])
                    elif 'events_received_total' in metric:
                        events_value = float(parts[1])
                    elif 'events_received_rate_per_second' in metric:
                        events_rate_value = float(parts[1])
            
            # Create a simplified protobuf message
            # WriteRequest contains repeated TimeSeries timeseries = 1
            # TimeSeries contains repeated Label labels = 1 and repeated Sample samples = 2
            # Label contains string name = 1 and string value = 2
            # Sample contains double value = 1 and int64 timestamp = 2
            
            buffer = io.BytesIO()
            
            # Create timeseries for memory
            if memory_value is not None:
                # TimeSeries message
                timeseries_data = self._create_timeseries_protobuf(
                    "marketdata_memory_usage_bytes", 
                    memory_value, 
                    timestamp_ms
                )
                # WriteRequest field 1 (timeseries)
                buffer.write(b'\x0a')  # field 1, wire type 2 (length-delimited)
                buffer.write(self._encode_varint(len(timeseries_data)))
                buffer.write(timeseries_data)
            
            # Create timeseries for events
            if events_value is not None:
                # TimeSeries message
                timeseries_data = self._create_timeseries_protobuf(
                    "marketdata_orderbook_events_received_total", 
                    events_value, 
                    timestamp_ms
                )
                # WriteRequest field 1 (timeseries)
                buffer.write(b'\x0a')  # field 1, wire type 2 (length-delimited)
                buffer.write(self._encode_varint(len(timeseries_data)))
                buffer.write(timeseries_data)
            
            # Create timeseries for events rate (for hill visualization)
            if events_rate_value is not None:
                # TimeSeries message
                timeseries_data = self._create_timeseries_protobuf(
                    "marketdata_orderbook_events_received_rate_per_second", 
                    events_rate_value, 
                    timestamp_ms
                )
                # WriteRequest field 1 (timeseries)
                buffer.write(b'\x0a')  # field 1, wire type 2 (length-delimited)
                buffer.write(self._encode_varint(len(timeseries_data)))
                buffer.write(timeseries_data)
            
            # Get protobuf data
            protobuf_data = buffer.getvalue()
            
            # Compress with snappy
            compressed = snappy.compress(protobuf_data)
            
            headers = {
                'Content-Type': 'application/x-protobuf',
                'Content-Encoding': 'snappy',
                'X-Prometheus-Remote-Write-Version': '0.1.0'
            }
            
            # Make request to Grafana Cloud
            response = requests.post(
                self.remote_write_url,
                data=compressed,
                headers=headers,
                auth=self.remote_write_auth,
                timeout=10
            )
            
            if response.status_code in [200, 204]:
                return True
            else:
                print(f"Remote write failed: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            print(f"Error sending to Grafana Cloud: {e}")
            return False
    
    def _encode_varint(self, value: int) -> bytes:
        """Encode integer as protobuf varint"""
        result = []
        while value > 0x7f:
            result.append((value & 0x7f) | 0x80)
            value >>= 7
        result.append(value & 0x7f)
        return bytes(result)
    
    def _create_timeseries_protobuf(self, metric_name: str, value: float, timestamp_ms: int) -> bytes:
        """Create TimeSeries protobuf message"""
        import io
        buffer = io.BytesIO()
        
        # Label for __name__
        label_data = self._create_label_protobuf("__name__", metric_name)
        buffer.write(b'\x0a')  # field 1, wire type 2 (length-delimited)
        buffer.write(self._encode_varint(len(label_data)))
        buffer.write(label_data)
        
        # Sample
        sample_data = self._create_sample_protobuf(value, timestamp_ms)
        buffer.write(b'\x12')  # field 2, wire type 2 (length-delimited)
        buffer.write(self._encode_varint(len(sample_data)))
        buffer.write(sample_data)
        
        return buffer.getvalue()
    
    def _create_label_protobuf(self, name: str, value: str) -> bytes:
        """Create Label protobuf message"""
        import io
        buffer = io.BytesIO()
        
        # name field (string)
        name_bytes = name.encode('utf-8')
        buffer.write(b'\x0a')  # field 1, wire type 2
        buffer.write(self._encode_varint(len(name_bytes)))
        buffer.write(name_bytes)
        
        # value field (string)
        value_bytes = value.encode('utf-8')
        buffer.write(b'\x12')  # field 2, wire type 2
        buffer.write(self._encode_varint(len(value_bytes)))
        buffer.write(value_bytes)
        
        return buffer.getvalue()
    
    def _create_sample_protobuf(self, value: float, timestamp_ms: int) -> bytes:
        """Create Sample protobuf message"""
        import io
        buffer = io.BytesIO()
        
        # value field (double)
        buffer.write(b'\x09')  # field 1, wire type 1 (fixed64)
        buffer.write(struct.pack('<d', value))  # little-endian double
        
        # timestamp field (int64)
        buffer.write(b'\x10')  # field 2, wire type 0 (varint)
        buffer.write(self._encode_varint(timestamp_ms))
        
        return buffer.getvalue()
    
    def stop(self):
        """Stop the metrics collector and all background threads"""
        print("📊 Stopping metrics collector...")
        self.shutdown_flag.set()
        
        # Wait for threads to finish (with timeout)
        if hasattr(self, 'remote_write_thread') and self.remote_write_thread.is_alive():
            self.remote_write_thread.join(timeout=2)
        
        if hasattr(self, 'rate_thread') and self.rate_thread.is_alive():
            self.rate_thread.join(timeout=2)
        
        print("📊 Metrics collector stopped")
    
    def restart(self):
        """Restart the metrics collector"""
        print("📊 Restarting metrics collector...")
        
        # Stop existing threads
        self.stop()
        
        # Reset shutdown flag
        self.shutdown_flag.clear()
        
        # Reset tracking variables
        self.last_events_count = 0
        self.last_rate_update = time.time()
        
        # Start new background threads
        if self.remote_write_enabled:
            self.remote_write_thread = threading.Thread(target=self._remote_write_loop, daemon=True)
            self.remote_write_thread.start()
            
        self.rate_thread = threading.Thread(target=self._rate_calculation_loop, daemon=True)
        self.rate_thread.start()
        
        print("📊 Metrics collector restarted")
        
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get human-readable metrics summary"""
        return {
            'memory_usage_mb': memory_usage._value.get() / (1024 * 1024),
            'queue_size': queue_size._value.get(),
            'queue_utilization_percent': queue_utilization._value.get(),
            'processing_rate_per_sec': processing_rate._value.get(),
            'orderbook_events_received': orderbook_events_received._value.get(),
            'orderbook_events_processed': orderbook_events_processed._value.get(),
            'staleness_incidents': {
                'warning': staleness_incidents.labels(severity='warning')._value.get(),
                'critical': staleness_incidents.labels(severity='critical')._value.get()
            }
        }

# Global metrics collector instance
metrics_collector = MetricsCollector()