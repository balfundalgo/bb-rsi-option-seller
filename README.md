# BB-RSI Option Seller v2.0

Bollinger Band + RSI based NIFTY/BANKNIFTY Option Selling Strategy

Built by [Balfund Trading Pvt Ltd](https://www.balfund.com)

## Architecture
- **WebSocket** for real-time spot + option LTP monitoring
- **Instant** trigger, SL, and target execution (no candle-close delay)
- **BB target** checked on candle close only
- **REST API** for historical warmup and option chain resolution
- ChartIQ-matching BB and RSI calculations

## Setup
```bash
pip install -r requirements.txt
python bb_rsi_seller.py
```

## Configuration
Fill `.env` with Dhan credentials or enter in GUI.
