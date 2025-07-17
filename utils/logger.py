"""
Logging configuration for MarketDataPublisher server
"""

import logging
import sys
import os
from datetime import datetime
from pythonjsonlogger import jsonlogger
from config import ServerConfig

def setup_logger(name: str) -> logging.Logger:
    """Setup a logger with JSON formatting"""
    
    logger = logging.getLogger(name)
    
    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger
    
    logger.setLevel(getattr(logging, ServerConfig.LOG_LEVEL.upper()))
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    
    # Create JSON formatter
    formatter = jsonlogger.JsonFormatter(
        fmt='%(asctime)s %(name)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(formatter)
    
    # Add handler to logger
    logger.addHandler(console_handler)
    
    return logger

def setup_data_logger() -> logging.Logger:
    """Setup a separate logger for orderbook data updates"""
    
    logger = logging.getLogger('orderbook_data')
    
    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.INFO)
    
    # Create logs directory if it doesn't exist
    os.makedirs('../logs', exist_ok=True)
    
    # Create file handler for data logs
    file_handler = logging.FileHandler('../logs/orderbook_data.log')
    file_handler.setLevel(logging.INFO)
    
    # Create JSON formatter for data logs
    formatter = jsonlogger.JsonFormatter(
        fmt='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S,%f'
    )
    file_handler.setFormatter(formatter)
    
    # Add handler to logger
    logger.addHandler(file_handler)
    
    return logger

def setup_system_logger() -> logging.Logger:
    """Setup a separate logger for system events"""
    
    logger = logging.getLogger('system_events')
    
    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.INFO)
    
    # Create logs directory if it doesn't exist
    os.makedirs('../logs', exist_ok=True)
    
    # Create file handler for system logs
    file_handler = logging.FileHandler('../logs/system_events.log')
    file_handler.setLevel(logging.INFO)
    
    # Create JSON formatter for system logs
    formatter = jsonlogger.JsonFormatter(
        fmt='%(asctime)s %(name)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S,%f'
    )
    file_handler.setFormatter(formatter)
    
    # Add handler to logger
    logger.addHandler(file_handler)
    
    return logger

def log_orderbook_update(data_logger: logging.Logger, system_logger: logging.Logger, orderbook_data: dict, processing_time_ms: float = None):
    """Log orderbook update to both data and system logs"""
    
    # Enhanced data for separate logging
    data_log = {
        "event": "orderbook_update",
        "pair": orderbook_data.get("pair"),
        "sequence_id": orderbook_data.get("sequence_id"),
        "timestamp_received": orderbook_data.get("timestamp_received"),
        "timestamp_parsed": orderbook_data.get("timestamp_parsed"),
        "timestamp_processed": orderbook_data.get("timestamp_processed"),
        "best_bid": orderbook_data.get("bids", [[]])[0][0] if orderbook_data.get("bids") else None,
        "best_ask": orderbook_data.get("asks", [[]])[0][0] if orderbook_data.get("asks") else None,
        "spread": orderbook_data.get("spread"),
        "mid_price": orderbook_data.get("mid_price"),
        "data_age_ms": orderbook_data.get("data_age_ms"),
        "processing_delay_ms": orderbook_data.get("processing_delay_ms"),
        "is_stale": orderbook_data.get("is_stale"),
        "queue_position": orderbook_data.get("queue_position")
    }
    
    # Log to data file
    data_logger.info("Orderbook data", extra=data_log)
    
    # Log to system file (simpler format)
    system_log = {
        "event": "orderbook_processed",
        "sequence_id": orderbook_data.get("sequence_id"),
        "processing_time_ms": processing_time_ms,
        "data_age_ms": orderbook_data.get("data_age_ms"),
        "is_stale": orderbook_data.get("is_stale"),
        "queue_size": orderbook_data.get("queue_position")
    }
    
    system_logger.info("Orderbook processed", extra=system_log)

def log_heartbeat(logger: logging.Logger, heartbeat_data: dict):
    """Log heartbeat with server metrics"""
    log_data = {
        "event": "heartbeat",
        "server_status": heartbeat_data.get("server_status"),
        "queue_size": heartbeat_data.get("queue_size"),
        "memory_usage_mb": heartbeat_data.get("memory_usage_mb"),
        "active_clients": heartbeat_data.get("active_clients"),
        "current_scenario": heartbeat_data.get("current_scenario")
    }
    
    logger.info("Heartbeat sent", extra=log_data)

def log_scenario_switch(logger: logging.Logger, old_scenario: str, new_scenario: str):
    """Log scenario switching"""
    log_data = {
        "event": "scenario_switch",
        "old_scenario": old_scenario,
        "new_scenario": new_scenario,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    logger.info("Scenario switched", extra=log_data)

def log_incident_alert(logger: logging.Logger, alert_type: str, details: dict):
    """Log incident alerts"""
    log_data = {
        "event": "incident_alert",
        "alert_type": alert_type,
        "details": details,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    logger.warning("Incident alert triggered", extra=log_data)

def log_server_metrics(logger: logging.Logger, metrics: dict):
    """Log server performance metrics"""
    log_data = {
        "event": "server_metrics",
        "uptime_seconds": metrics.get("uptime_seconds"),
        "total_messages_processed": metrics.get("total_messages_processed"),
        "queue_size": metrics.get("queue_size"),
        "memory_usage_mb": metrics.get("memory_usage_mb"),
        "active_clients": metrics.get("active_clients"),
        "processing_rate_per_sec": metrics.get("processing_rate_per_sec")
    }
    
    logger.info("Server metrics", extra=log_data) 