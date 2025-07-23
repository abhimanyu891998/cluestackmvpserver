"""
Data loader for market data processing
"""

import json
import asyncio
from pathlib import Path
from typing import Dict, List, Any, Optional, AsyncGenerator
from datetime import datetime, timezone
import time

from config import ServerConfig
from models import InternalOrderbook, OrderbookLevel
from utils.logger import setup_logger

logger = setup_logger(__name__)

class MarketDataLoader:
    """Load and manage market data feeds"""
    
    def __init__(self):
        self.data_dir = Path(ServerConfig.DATA_DIR)
        self.scenarios = {}
        self.current_scenario = ServerConfig.INITIAL_SCENARIO
        self.current_scenario_data = None
        self.current_update_index = 0
        
    def load_scenario(self, scenario_name: str) -> bool:
        """Load a specific scenario from file"""
        if scenario_name not in ServerConfig.SCENARIOS:
            logger.error(f"Scenario '{scenario_name}' not found")
            return False
        
        scenario_file = self.data_dir / ServerConfig.SCENARIOS[scenario_name]
        
        if not scenario_file.exists():
            logger.error(f"Scenario file not found: {scenario_file}")
            return False
        
        try:
            with open(scenario_file, 'r') as f:
                scenario_data = json.load(f)
            
            self.scenarios[scenario_name] = scenario_data
            logger.info(f"Loaded scenario '{scenario_name}' with {len(scenario_data['updates'])} updates")
            return True
            
        except Exception as e:
            logger.error(f"Error loading scenario '{scenario_name}': {e}")
            return False
    
    def load_all_scenarios(self) -> bool:
        """Load all available scenarios"""
        success = True
        for scenario_name in ServerConfig.SCENARIOS.keys():
            if not self.load_scenario(scenario_name):
                success = False
        
        if success:
            logger.info(f"Loaded {len(self.scenarios)} scenarios successfully")
        
        return success
    
    def switch_scenario(self, scenario_name: str) -> bool:
        """Switch to a different scenario"""
        if scenario_name not in self.scenarios:
            if not self.load_scenario(scenario_name):
                return False
        
        old_scenario = self.current_scenario
        self.current_scenario = scenario_name
        self.current_scenario_data = self.scenarios[scenario_name]
        self.current_update_index = 0
        
        logger.info(f"Switched from '{old_scenario}' to '{scenario_name}'")
        return True
    
    def get_current_scenario_info(self) -> Dict[str, Any]:
        """Get information about the current scenario"""
        if not self.current_scenario_data:
            return {}
        
        scenario = self.current_scenario_data['scenario']
        metadata = self.current_scenario_data['metadata']
        
        return {
            "name": self.current_scenario,
            "description": scenario.get('description', ''),
            "total_updates": metadata.get('totalUpdates', 0),
            "duration_ms": metadata.get('duration', 0),
            "current_update_index": self.current_update_index,
            "progress_percent": (self.current_update_index / metadata.get('totalUpdates', 1)) * 100 if metadata.get('totalUpdates', 0) > 0 else 0
        }
    
    def get_next_update(self, loop_on_end: bool = True) -> Optional[Dict[str, Any]]:
        """Get the next update from the current scenario"""
        if not self.current_scenario_data:
            return None
        
        updates = self.current_scenario_data['updates']
        
        if self.current_update_index >= len(updates):
            if loop_on_end:
                self.current_update_index = 0
            else:
                return None
        
        update = updates[self.current_update_index]
        self.current_update_index += 1
        
        return update
    
    def reset_scenario(self):
        """Reset the current scenario to the beginning"""
        self.current_update_index = 0
        logger.info(f"Reset scenario '{self.current_scenario}' to beginning")
    
    def get_scenario_progress(self) -> Dict[str, Any]:
        """Get progress information for the current scenario"""
        if not self.current_scenario_data:
            return {"error": "No scenario loaded"}
        
        metadata = self.current_scenario_data['metadata']
        total_updates = metadata.get('totalUpdates', 0)
        
        return {
            "scenario": self.current_scenario,
            "current_index": self.current_update_index,
            "total_updates": total_updates,
            "progress_percent": (self.current_update_index / total_updates) * 100 if total_updates > 0 else 0,
            "remaining_updates": max(0, total_updates - self.current_update_index)
        }

class OrderbookParser:
    """Parse Binance WebSocket format into internal orderbook structure"""
    
    @staticmethod
    def parse_binance_orderbook(binance_data: Dict[str, Any], pair: str = "BTCUSDT") -> InternalOrderbook:
        """Parse Binance WebSocket orderbook data into internal format"""
        
        # Extract data from Binance format
        data = binance_data.get('data', {})
        sequence_id = data.get('lastUpdateId', 0)
        bids_raw = data.get('bids', [])
        asks_raw = data.get('asks', [])
        
        # Parse timestamp
        timestamp_received = datetime.now(timezone.utc)
        
        # Convert to OrderbookLevel objects (top 15 levels)
        bids = [
            OrderbookLevel(price=bid[0], quantity=bid[1])
            for bid in bids_raw[:ServerConfig.TOP_LEVELS]
        ]
        
        asks = [
            OrderbookLevel(price=ask[0], quantity=ask[1])
            for ask in asks_raw[:ServerConfig.TOP_LEVELS]
        ]
        
        # Create internal orderbook
        orderbook = InternalOrderbook(
            pair=pair,
            sequence_id=sequence_id,
            timestamp_received=timestamp_received,
            timestamp_parsed=datetime.now(timezone.utc),
            timestamp_processed=None,
            bids=bids,
            asks=asks,
            spread=None,
            mid_price=None,
            data_age_ms=None,
            processing_delay_ms=None
        )
        
        # Calculate derived fields
        orderbook.calculate_derived_fields()
        
        return orderbook
    
    @staticmethod
    def validate_orderbook_data(orderbook: InternalOrderbook) -> bool:
        """Validate orderbook data integrity"""
        try:
            # Check basic structure
            if not orderbook.bids or not orderbook.asks:
                return False
            
            # Check sequence ID
            if orderbook.sequence_id <= 0:
                return False
            
            # Check price ordering (bids descending, asks ascending)
            bid_prices = [float(bid.price) for bid in orderbook.bids]
            ask_prices = [float(ask.price) for ask in orderbook.asks]
            
            if bid_prices != sorted(bid_prices, reverse=True):
                return False
            
            if ask_prices != sorted(ask_prices):
                return False
            
            # Check spread
            if orderbook.spread and orderbook.spread <= 0:
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Orderbook validation error: {e}")
            return False

class DataPublisher:
    """Publish real-time market data feed"""
    
    def __init__(self, data_loader: MarketDataLoader, parser: OrderbookParser):
        self.data_loader = data_loader
        self.parser = parser
        self.is_running = False
        self.publish_speed = 1.0  # Publishing speed multiplier (1.0 = real-time)
        
    async def start_publishing(self, scenario_name: str, speed_multiplier: float = 1.0, loop_continuously: bool = True):
        """Start publishing data feed for a scenario"""
        if not self.data_loader.switch_scenario(scenario_name):
            logger.error(f"Failed to switch to scenario: {scenario_name}")
            return
        
        self.publish_speed = speed_multiplier
        self.is_running = True
        
        scenario_info = self.data_loader.get_current_scenario_info()
        logger.info(f"Starting data publishing for '{scenario_name}': {scenario_info['total_updates']} updates (looping: {loop_continuously})")
        
        # Calculate timing based on scenario
        if not self.data_loader.current_scenario_data:
            logger.error("No scenario data available")
            return
        
        scenario = self.data_loader.current_scenario_data['scenario']
        total_duration = scenario.get('duration', 10000)  # milliseconds
        total_updates = scenario_info['total_updates']
        
        if total_updates > 0:
            avg_interval = (total_duration / total_updates) / 1000.0  # seconds
            adjusted_interval = avg_interval / speed_multiplier
        else:
            adjusted_interval = 0.1  # default 100ms
        
        logger.info(f"Publishing timing: {adjusted_interval:.3f}s between updates (speed: {speed_multiplier}x)")
        
        while self.is_running:
            update = self.data_loader.get_next_update(loop_on_end=loop_continuously)
            
            if update is None:
                if not loop_continuously:
                    logger.info(f"Data publishing completed for scenario: {scenario_name}")
                    break
                else:
                    # This shouldn't happen if looping is enabled, but just in case
                    logger.debug(f"End of data for scenario: {scenario_name}, stopping")
                    break
            
            # Parse the update
            try:
                orderbook = self.parser.parse_binance_orderbook(update)
                
                if self.parser.validate_orderbook_data(orderbook):
                    # Yield the parsed orderbook
                    yield orderbook
                else:
                    logger.warning(f"Invalid orderbook data at sequence {orderbook.sequence_id}")
                    
            except Exception as e:
                logger.error(f"Error parsing update: {e}")
            
            # Wait for next update
            if adjusted_interval > 0:
                await asyncio.sleep(adjusted_interval)
    
    def stop_publishing(self):
        """Stop the data publishing"""
        self.is_running = False
        logger.info("Data publishing stopped")
    
    def get_publishing_status(self) -> Dict[str, Any]:
        """Get current publishing status"""
        progress = self.data_loader.get_scenario_progress()
        
        return {
            "is_running": self.is_running,
            "speed_multiplier": self.publish_speed,
            "progress": progress
        } 