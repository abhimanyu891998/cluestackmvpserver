# MarketDataPublisher Server

Real-time orderbook data distribution service for cryptocurrency trading systems.

## Features

- WebSocket-based real-time orderbook streaming
- Configurable processing scenarios for different market conditions
- Queue-based message processing with performance monitoring
- System health monitoring and alerting
- RESTful API for configuration and status

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Server configuration is managed through `config.py`:

- `HOST`: Server host address (default: localhost)
- `PORT`: Server port (default: 8000)
- `MAX_QUEUE_SIZE`: Maximum queue size for message processing
- `PROCESSING_DELAY_MS`: Base processing delay in milliseconds
- `MEMORY_THRESHOLD_MB`: Memory threshold for performance monitoring

## Running the Server

```bash
python main.py
```

## API Endpoints

- `GET /status` - Server health status
- `GET /config/profiles` - List available market scenarios
- `POST /config/profile/{profile_name}` - Switch market scenario
- `WebSocket /ws` - Real-time orderbook data stream

## Market Scenarios

- **stable-mode**: Normal market conditions
- **burst-mode**: High-frequency trading periods
- **gradual-spike**: Progressive market volatility
- **extreme-spike**: Maximum market stress conditions

## Monitoring

System metrics are logged to:
- `logs/system_events.log` - System events and performance metrics
- `logs/orderbook_data.log` - Orderbook processing data