"""
Data models for MarketDataPublisher server
"""

from pydantic import BaseModel, Field
from typing import List, Tuple, Optional
from datetime import datetime
import time

class OrderbookLevel(BaseModel):
    """Single orderbook level (bid or ask)"""
    price: str
    quantity: str
    
    def to_tuple(self) -> Tuple[str, str]:
        """Convert to tuple format for compatibility"""
        return (self.price, self.quantity)

class InternalOrderbook(BaseModel):
    """Internal orderbook data structure"""
    pair: str = Field(..., description="Trading pair name")
    sequence_id: int = Field(..., description="Update sequence ID")
    timestamp_received: datetime = Field(..., description="When data was received from exchange")
    timestamp_parsed: datetime = Field(..., description="When data was parsed internally")
    timestamp_processed: Optional[datetime] = Field(None, description="When data was processed and sent to clients")
    bids: List[OrderbookLevel] = Field(..., description="Top 15 bid levels")
    asks: List[OrderbookLevel] = Field(..., description="Top 15 ask levels")
    spread: Optional[float] = Field(None, description="Current spread")
    mid_price: Optional[float] = Field(None, description="Mid price")
    data_age_ms: Optional[float] = Field(None, description="Age of data when processed")
    processing_delay_ms: Optional[float] = Field(None, description="Processing delay for this message")
    
    def calculate_derived_fields(self):
        """Calculate spread and mid price"""
        if self.bids and self.asks:
            best_bid = float(self.bids[0].price)
            best_ask = float(self.asks[0].price)
            self.spread = best_ask - best_bid
            self.mid_price = (best_bid + best_ask) / 2
    
    def calculate_data_age(self):
        """Calculate how old the data is when processed"""
        if self.timestamp_processed and self.timestamp_received:
            age_delta = self.timestamp_processed - self.timestamp_received
            self.data_age_ms = age_delta.total_seconds() * 1000
    
    def to_dict(self) -> dict:
        """Convert to dictionary for WebSocket transmission"""
        return {
            "pair": self.pair,
            "sequence_id": self.sequence_id,
            "timestamp_received": self.timestamp_received.isoformat(),
            "timestamp_parsed": self.timestamp_parsed.isoformat(),
            "timestamp_processed": self.timestamp_processed.isoformat() if self.timestamp_processed else None,
            "bids": [level.to_tuple() for level in self.bids],
            "asks": [level.to_tuple() for level in self.asks],
            "spread": self.spread,
            "mid_price": self.mid_price,
            "data_age_ms": self.data_age_ms,
            "processing_delay_ms": self.processing_delay_ms
        }

class HeartbeatMessage(BaseModel):
    """Heartbeat message for client monitoring"""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    server_status: str = Field(default="healthy")
    queue_size: int = Field(default=0)
    memory_usage_mb: float = Field(default=0.0)
    active_clients: int = Field(default=0)
    current_scenario: str = Field(default="stable-mode")

class ServerStatus(BaseModel):
    """Server status information"""
    status: str = Field(default="running")
    uptime_seconds: float = Field(default=0.0)
    total_messages_processed: int = Field(default=0)
    current_queue_size: int = Field(default=0)
    memory_usage_mb: float = Field(default=0.0)
    active_clients: int = Field(default=0)
    current_scenario: str = Field(default="stable-mode")
    last_heartbeat: Optional[datetime] = Field(default=None)

class WebSocketMessage(BaseModel):
    """Base WebSocket message structure"""
    type: str = Field(..., description="Message type")
    data: dict = Field(..., description="Message data")
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class OrderbookMessage(WebSocketMessage):
    """Orderbook update message"""
    type: str = Field(default="orderbook_update")
    data: dict = Field(..., description="Orderbook data")

class HeartbeatWebSocketMessage(WebSocketMessage):
    """Heartbeat WebSocket message"""
    type: str = Field(default="heartbeat")
    data: dict = Field(..., description="Heartbeat data")

class AlertMessage(WebSocketMessage):
    """Alert message for client notifications"""
    type: str = Field(default="alert")
    data: dict = Field(..., description="Alert data") 