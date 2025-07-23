"""
Configuration settings for MarketDataPublisher server
"""

import os
from typing import Dict, Any
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class ServerConfig:
    """Server configuration settings"""
    
    # Server settings
    HOST = os.getenv("HOST", "127.0.0.1")  # Changed from 0.0.0.0 to 127.0.0.1
    PORT = int(os.getenv("PORT", 8000))
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"
    
    # WebSocket settings
    WS_PING_INTERVAL = 20  # seconds
    WS_PING_TIMEOUT = 10   # seconds
    
    # Data processing settings
    MAX_QUEUE_SIZE = 10000
    PROCESSING_DELAY_MS = 50  # Processing delay for message batching
    TOP_LEVELS = 15  # Number of orderbook levels to publish
    
    # Performance settings
    INITIAL_SCENARIO = "stable-mode"
    PROFILE_SWITCH_TIME = 10  # seconds before switching performance profiles
    MEMORY_THRESHOLD_MB = 150  # Memory threshold for performance monitoring
    GRACEFUL_SHUTDOWN_DELAY = 10  # seconds warning before shutdown
    
    # Heartbeat settings
    HEARTBEAT_INTERVAL = 1  # seconds
    HEARTBEAT_TIMEOUT = 2   # seconds (client alert threshold)
    
    # Logging settings
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # Grafana Cloud settings (set via environment variables)
    LOKI_URL = os.getenv('LOKI_URL', '')
    LOKI_USERNAME = os.getenv('LOKI_USERNAME', '')
    LOKI_PASSWORD = os.getenv('LOKI_PASSWORD', '')
    ENABLE_LOKI = bool(True)
    
    # Prometheus Remote Write settings
    PROMETHEUS_URL = os.getenv('PROMETHEUS_URL', '')
    PROMETHEUS_USERNAME = os.getenv('PROMETHEUS_USERNAME', '')
    PROMETHEUS_PASSWORD = os.getenv('PROMETHEUS_PASSWORD', '')
    ENABLE_PROMETHEUS_REMOTE_WRITE = bool(True)
    
    # Data paths
    DATA_DIR = "../data/generated"
    SCENARIOS = {
        "stable-mode": "stable-mode-data.json",
        "burst-mode": "burst-mode-data.json", 
        "gradual-spike": "gradual-spike-data.json",
        "extreme-spike": "extreme-spike-data.json"
    }
    
    # Trading pair settings
    TRADING_PAIR = "BTCUSDT"
    BASE_PRICE = 120000.0

class PerformanceConfig:
    """Configuration for performance profile scenarios"""
    
    @staticmethod
    def get_scenario_sequence() -> list:
        """Get the sequence of performance profiles"""
        return [
            {
                "name": "stable-mode",
                "duration": 10,  # seconds
                "description": "Normal operation"
            },
            {
                "name": "burst-mode", 
                "duration": 60,  # seconds
                "description": "High-frequency market spike"
            }
        ]
    
    @staticmethod
    def get_processing_delays() -> Dict[str, int]:
        """Get processing delays for different market conditions (in milliseconds)"""
        return {
            "stable-mode": 20,    # Normal processing
            "burst-mode": 80,     # Processing delay under high load
            "gradual-spike": 60,  # Moderate delay
            "extreme-spike": 120  # Maximum delay under extreme load
        } 