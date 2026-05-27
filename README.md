# BB-RSI Option Seller v1.0

**Bollinger Band + RSI based NIFTY/BANKNIFTY Option Selling Strategy**

Built by [Balfund Trading Pvt Ltd](https://www.balfund.com)

## Strategy Logic

### CE SELL Setup (Bearish Signal)
1. **Alert Candle**: Opens ABOVE upper Bollinger Band, closes INSIDE the bands (below upper, above lower)
2. **Trigger**: Very next candle breaks below alert candle's low → SELL CE immediately
3. If trigger candle doesn't break the low → setup cancelled, wait for fresh alert

### PE SELL Setup (Bullish Signal)
1. **Alert Candle**: Opens BELOW lower Bollinger Band, closes INSIDE the bands (above lower, below upper)
2. **Trigger**: Very next candle breaks above alert candle's high → SELL PE immediately
3. If trigger candle doesn't break the high → setup cancelled, wait for fresh alert

### Filters & Risk Management
- **RSI Filter**: Option strike RSI must be ≥ 70 (configurable, toggleable)
- **Stop Loss**: Alert candle high/low + buffer points (5), capped at max 50 points
- **BB Target**: CE exit when price closes/goes below lower BB; PE exit when price closes/goes above upper BB
- **Universal Target**: 70 points profit (configurable)
- **Profit Trailing**: Multi-step ladder (10pts profit → lock 6pts, 20→16, 30→26, etc.)
- **Time Filters**: No new trades after 14:45, force exit at 15:05 (all configurable)

### Indicators (ChartIQ / Dhan matched)
- **Bollinger Bands**: SMA(20) ± 2×PopulationStdDev
- **RSI**: Wilder smoothed (RMA), period 14

## Setup

### Download EXE (Easiest)
Download from GitHub Actions artifacts — no Python needed.

### Run from Source
```bash
pip install -r requirements.txt
python bb_rsi_seller.py
```

### Configuration
1. Fill `.env` with your Dhan credentials (or enter in the GUI)
2. Select Index, Timeframe, Strike Offset, Lots
3. Configure SL, Target, RSI filter, Trailing
4. Click Connect → Start

## Build EXE Locally
```bash
pip install pyinstaller
python bundler.py
```

## Architecture
- REST polling (5s interval) for exchange-accurate OHLC
- 1-min candle aggregation to any timeframe (1min to 4hr)
- ChartIQ-matching BB and RSI calculations
- Dhan API v2 for orders, option chain, historical data
- Auto token generation via TOTP
- CustomTkinter dark-mode GUI
- Dated log files in `logs/` directory
