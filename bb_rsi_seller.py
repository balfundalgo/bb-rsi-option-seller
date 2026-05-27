#!/usr/bin/env python3
"""
Balfund BB-RSI Option Seller v1.0
Bollinger Band + RSI based NIFTY/BANKNIFTY Option Selling Strategy
Dhan API | REST Polling | CustomTkinter Modern UI

Author: Balfund Trading Pvt Ltd (www.balfund.com)
"""

import os, sys, io, csv, json, time, math, struct, signal, threading, logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple
from pathlib import Path
from collections import deque

# Fix Windows console encoding
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import requests, pyotp
from dotenv import load_dotenv, set_key
import customtkinter as ctk

# ═══════════════════════════════════════════════════════════
# CONSTANTS & CONFIG
# ═══════════════════════════════════════════════════════════
IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = "https://api.dhan.co/v2"
AUTH_GENERATE_URL = "https://auth.dhan.co/app/generateAccessToken"
AUTH_RENEW_URL = "https://api.dhan.co/v2/RenewToken"
AUTH_VERIFY_URL = "https://api.dhan.co/v2/profile"
DHAN_INSTRUMENT_API = "https://api.dhan.co/v2/instrument"
DHAN_COMPACT_CSV = "https://images.dhan.co/api-data/api-scrip-master.csv"

HEADERS: Dict[str, str] = {}
DHAN_CLIENT_ID = ""
DHAN_ACCESS_TOKEN = ""

INDEX_MAP = {
    "NIFTY": {"security_id": "13", "strike_gap": 50, "lot_size": 75, "segment": "IDX_I"},
    "BANKNIFTY": {"security_id": "25", "strike_gap": 100, "lot_size": 30, "segment": "IDX_I"},
}

TIMEFRAME_MAP = {
    "1 Minute": ("1", 1),
    "3 Minutes": ("1", 3),
    "5 Minutes": ("5", 5),
    "15 Minutes": ("15", 15),
    "30 Minutes": ("30", 30),
    "1 Hour": ("60", 60),
    "2 Hours": ("60", 120),
    "4 Hours": ("60", 240),
}

# ═══════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, f"bb_rsi_{datetime.now().strftime('%Y%m%d')}.log")
file_handler = logging.FileHandler(log_file, encoding='utf-8')
stream_handler = logging.StreamHandler()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[file_handler, stream_handler]
)
log = logging.getLogger("BB_RSI")

# ═══════════════════════════════════════════════════════════
# ENV FILE
# ═══════════════════════════════════════════════════════════
BASE_DIR = Path(os.path.dirname(sys.executable)) if getattr(sys, 'frozen', False) else Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
if not ENV_FILE.exists():
    ENV_FILE.write_text("DHAN_CLIENT_ID=\nDHAN_PIN=\nDHAN_TOTP_SECRET=\nDHAN_ACCESS_TOKEN=\n")
load_dotenv(str(ENV_FILE), override=True)


# ═══════════════════════════════════════════════════════════
# DHAN TOKEN MANAGER
# ═══════════════════════════════════════════════════════════
class DhanTokenManager:
    def __init__(self, client_id, pin, totp_secret, existing_token=""):
        self.client_id = client_id
        self.pin = pin
        self.totp_secret = totp_secret
        self.existing_token = existing_token

    def verify(self, token):
        if not token:
            return False
        try:
            r = requests.get(AUTH_VERIFY_URL, headers={"access-token": token, "client-id": self.client_id}, timeout=10)
            return r.status_code == 200
        except:
            return False

    def renew(self, token):
        try:
            d = requests.get(AUTH_RENEW_URL, headers={
                "access-token": token, "dhanClientId": self.client_id,
                "Content-Type": "application/json"
            }, timeout=15).json()
            if "accessToken" in d:
                log.info("Token renewed")
                return d["accessToken"]
        except:
            pass
        return None

    def generate(self, max_retries=3):
        for attempt in range(max_retries):
            rem = 30 - (int(time.time()) % 30)
            if attempt > 0 or rem < 10:
                log.info(f"Waiting {rem+1}s for TOTP...")
                time.sleep(rem + 1)
            totp = pyotp.TOTP(self.totp_secret).now()
            log.info(f"Attempt {attempt+1}: TOTP={totp}")
            try:
                d = requests.post(AUTH_GENERATE_URL, params={
                    "dhanClientId": self.client_id, "pin": self.pin, "totp": totp
                }, timeout=15).json()
                if "accessToken" in d:
                    log.info("Token generated")
                    return d["accessToken"]
                err = str(d.get("errorMessage") or d)
                log.warning(f"Generate failed: {err}")
                if "totp" in err.lower():
                    continue
                return None
            except Exception as e:
                log.warning(f"Generate exception: {e}")
                time.sleep(2)
        return None

    def ensure_token(self):
        if self.existing_token:
            log.info("Verifying existing token...")
            if self.verify(self.existing_token):
                log.info("Token valid")
                return self.existing_token
            r = self.renew(self.existing_token)
            if r:
                self._save(r)
                return r
        t = self.generate()
        if not t:
            log.error("Could not obtain token")
            return None
        self._save(t)
        return t

    def _save(self, t):
        try:
            set_key(str(ENV_FILE), "DHAN_ACCESS_TOKEN", t)
            log.info("Token saved to .env")
        except:
            pass


def init_credentials():
    global HEADERS, DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID
    DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
    pin = os.getenv("DHAN_PIN", "").strip()
    totp_secret = os.getenv("DHAN_TOTP_SECRET", "").strip()
    DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()

    if not all([DHAN_CLIENT_ID, pin, totp_secret]):
        return False, "Missing DHAN_CLIENT_ID, DHAN_PIN, or DHAN_TOTP_SECRET in .env"

    log.info("Authenticating...")
    tm = DhanTokenManager(DHAN_CLIENT_ID, pin, totp_secret, DHAN_ACCESS_TOKEN)
    token = tm.ensure_token()
    if not token:
        return False, "Failed to obtain Dhan access token"
    DHAN_ACCESS_TOKEN = token
    HEADERS.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": token,
        "client-id": DHAN_CLIENT_ID
    })
    return True, "Connected"


# ═══════════════════════════════════════════════════════════
# API HELPERS
# ═══════════════════════════════════════════════════════════
def now_ist():
    return datetime.now(IST)


def api_post(ep, payload, retries=2):
    url = f"{BASE_URL}{ep}"
    for a in range(retries + 1):
        try:
            r = requests.post(url, headers=HEADERS, json=payload, timeout=15)
            if r.status_code == 200:
                return r.json()
            log.warning(f"API {ep} -> {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log.error(f"API {ep} err: {e}")
        if a < retries:
            time.sleep(1)
    return None


def api_get(ep, retries=2):
    url = f"{BASE_URL}{ep}"
    for a in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            log.warning(f"GET {ep} -> {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log.error(f"GET {ep} err: {e}")
        if a < retries:
            time.sleep(1)
    return None


def _norm_epoch(ts):
    ts = int(ts)
    d = ts - int(time.time())
    if 16200 <= d <= 23400:
        ts -= 19800
    return ts


# ═══════════════════════════════════════════════════════════
# HISTORICAL DATA FETCH
# ═══════════════════════════════════════════════════════════
def fetch_historical_1min(sid, segment, days=5):
    """Fetch 1-minute candles from Dhan for an index or FNO instrument."""
    to_d = now_ist().strftime("%Y-%m-%d")
    fr_d = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")

    # For index, use IDX_I segment and UNDIND instrument
    if segment == "IDX_I":
        inst_type = "UNDIND"
    else:
        inst_type = "OPTIDX"

    resp = api_post("/charts/intraday", {
        "securityId": str(sid),
        "exchangeSegment": segment,
        "instrument": inst_type,
        "interval": "1",
        "fromDate": fr_d,
        "toDate": to_d
    })
    if not resp or not resp.get("open"):
        log.warning(f"No historical data for {sid}")
        return []
    candles = []
    ts_list = resp.get("timestamp") or resp.get("start_Time") or []
    for i in range(len(resp["open"])):
        t = _norm_epoch(int(ts_list[i])) if i < len(ts_list) else 0
        candles.append({
            "timestamp": t,
            "open": float(resp["open"][i]),
            "high": float(resp["high"][i]),
            "low": float(resp["low"][i]),
            "close": float(resp["close"][i]),
        })
    log.info(f"Fetched {len(candles)} 1-min candles for {segment}:{sid}")
    return candles


def fetch_option_1min(sid, days=2):
    """Fetch 1-minute candles for an option strike from Dhan."""
    return fetch_historical_data_generic(sid, "NSE_FNO", "OPTIDX", days)


def fetch_historical_data_generic(sid, segment, instrument, days=5):
    to_d = now_ist().strftime("%Y-%m-%d")
    fr_d = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
    resp = api_post("/charts/intraday", {
        "securityId": str(sid),
        "exchangeSegment": segment,
        "instrument": instrument,
        "interval": "1",
        "fromDate": fr_d,
        "toDate": to_d
    })
    if not resp or not resp.get("open"):
        return []
    candles = []
    ts_list = resp.get("timestamp") or resp.get("start_Time") or []
    for i in range(len(resp["open"])):
        t = _norm_epoch(int(ts_list[i])) if i < len(ts_list) else 0
        candles.append({
            "timestamp": t,
            "open": float(resp["open"][i]),
            "high": float(resp["high"][i]),
            "low": float(resp["low"][i]),
            "close": float(resp["close"][i]),
        })
    return candles


# ═══════════════════════════════════════════════════════════
# OPTION CHAIN HELPERS
# ═══════════════════════════════════════════════════════════
def get_expiry_list(idx_name, expiry_choice="current"):
    """Get expiry dates. expiry_choice: 'current' or 'next'."""
    idx_info = INDEX_MAP.get(idx_name)
    if not idx_info:
        return None
    resp = api_post("/optionchain/expirylist", {
        "UnderlyingScrip": int(idx_info["security_id"]),
        "UnderlyingSeg": "IDX_I"
    })
    if not resp or resp.get("status") != "success":
        return None
    today = now_ist().date()
    valid = []
    for e in resp.get("data", []):
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
            if d >= today:
                valid.append((d, e))
        except:
            continue
    valid.sort()
    if not valid:
        return None
    if expiry_choice == "next" and len(valid) > 1:
        return valid[1][1]
    return valid[0][1]


def resolve_option_strike(idx_name, option_type, strike_offset=0, expiry_choice="current"):
    """
    Resolve option strike security_id from option chain.
    option_type: 'CE' or 'PE'
    strike_offset: offset from ATM in strike_gap units (e.g., 0=ATM, 1=ATM+1gap, -1=ATM-1gap)
    """
    idx_info = INDEX_MAP.get(idx_name)
    if not idx_info:
        return None

    expiry = get_expiry_list(idx_name, expiry_choice)
    if not expiry:
        log.error(f"{idx_name}: No expiry found")
        return None

    resp = api_post("/optionchain", {
        "UnderlyingScrip": int(idx_info["security_id"]),
        "UnderlyingSeg": "IDX_I",
        "Expiry": expiry
    })
    if not resp or resp.get("status") != "success":
        log.error(f"{idx_name}: Option chain fetch failed")
        return None

    spot = float(resp["data"]["last_price"])
    oc = resp["data"]["oc"]
    strike_gap = idx_info["strike_gap"]
    atm = round(spot / strike_gap) * strike_gap
    target_strike = atm + (strike_offset * strike_gap)

    key = None
    for k in oc:
        try:
            if abs(float(k) - target_strike) < 0.01:
                key = k
                break
        except:
            continue

    if not key:
        log.error(f"{idx_name}: Strike {target_strike} {option_type} not in OC")
        return None

    ok = "ce" if option_type == "CE" else "pe"
    if key not in oc or ok not in oc[key]:
        return None

    od = oc[key][ok]
    result = {
        "security_id": str(od["security_id"]),
        "strike": target_strike,
        "option_type": option_type,
        "last_price": float(od.get("last_price", 0)),
        "expiry": expiry,
        "spot": spot,
    }
    log.info(f"Resolved {idx_name} {int(target_strike)}{option_type} | SecID={result['security_id']} | LTP={result['last_price']:.2f}")
    return result


# ═══════════════════════════════════════════════════════════
# ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════
ORDER_MAX_RETRIES = 3
ORDER_RETRY_DELAY = 1.0
FILL_POLL_ATTEMPTS = 10
FILL_POLL_DELAY = 0.5


def place_order(security_id, exchange_segment, qty, buy_sell, product="INTRADAY"):
    payload = {
        "dhanClientId": DHAN_CLIENT_ID,
        "transactionType": buy_sell,
        "exchangeSegment": exchange_segment,
        "productType": product,
        "orderType": "MARKET",
        "validity": "DAY",
        "securityId": str(security_id),
        "quantity": int(qty),
        "price": 0, "triggerPrice": 0,
        "disclosedQuantity": 0,
        "afterMarketOrder": False,
    }
    order_id = None
    for attempt in range(ORDER_MAX_RETRIES):
        log.info(f"ORDER | {buy_sell} {qty} | SecID={security_id} | {exchange_segment} | Attempt {attempt+1}")
        resp = api_post("/orders", payload, retries=0)
        if resp and resp.get("orderId"):
            order_id = str(resp["orderId"])
            log.info(f"  Order placed | ID={order_id} | Status={resp.get('orderStatus', '?')}")
            break
        err = resp if resp else "No response"
        log.error(f"  Order failed: {err}")
        if attempt < ORDER_MAX_RETRIES - 1:
            time.sleep(ORDER_RETRY_DELAY)

    if not order_id:
        log.error(f"ORDER FAILED after {ORDER_MAX_RETRIES} attempts | {buy_sell} {qty} {security_id}")
        return None, 0.0

    fill_price = 0.0
    for poll in range(FILL_POLL_ATTEMPTS):
        time.sleep(FILL_POLL_DELAY)
        trades = api_get(f"/trades/{order_id}", retries=0)
        if trades and isinstance(trades, list) and len(trades) > 0:
            total_qty = 0
            total_val = 0.0
            for t in trades:
                tq = int(t.get("tradedQuantity", 0))
                tp = float(t.get("tradedPrice", 0))
                total_qty += tq
                total_val += tq * tp
            if total_qty > 0:
                fill_price = total_val / total_qty
                log.info(f"  Fill confirmed | Price={fill_price:.2f} | Qty={total_qty}")
                break
        order_info = api_get(f"/orders/{order_id}", retries=0)
        if order_info:
            status = order_info.get("orderStatus", "")
            if status in ("REJECTED", "CANCELLED"):
                log.error(f"  Order {status}: {order_info.get('omsErrorDescription', '?')}")
                return None, 0.0

    return order_id, fill_price


# ═══════════════════════════════════════════════════════════
# INDICATOR ENGINE (ChartIQ-matching)
# ═══════════════════════════════════════════════════════════
class IndicatorEngine:
    """
    Calculates Bollinger Bands and RSI matching ChartIQ / Dhan exactly.
    BB: SMA + population StdDev
    RSI: Wilder smoothing (RMA)
    """

    @staticmethod
    def calculate_bb(closes, period=20, multiplier=2.0):
        """Returns (upper, mid, lower) arrays. NaN where insufficient data."""
        import numpy as np
        n = len(closes)
        bb_mid = [float('nan')] * n
        bb_upper = [float('nan')] * n
        bb_lower = [float('nan')] * n

        for i in range(period - 1, n):
            window = closes[i - period + 1: i + 1]
            sma = sum(window) / period
            variance = sum((x - sma) ** 2 for x in window) / period
            sd = math.sqrt(variance)
            bb_mid[i] = sma
            bb_upper[i] = sma + multiplier * sd
            bb_lower[i] = sma - multiplier * sd

        return bb_upper, bb_mid, bb_lower

    @staticmethod
    def calculate_rsi(closes, period=14):
        """Returns RSI array. NaN where insufficient data. Wilder smoothing."""
        n = len(closes)
        rsi = [float('nan')] * n
        if n < period + 1:
            return rsi

        # Price changes
        d = [closes[i] - closes[i - 1] for i in range(1, n)]
        p = [max(0, x) for x in d]
        n_abs = [abs(min(0, x)) for x in d]

        # Seed with simple average
        avgP = sum(p[:period]) / period
        avgL = sum(n_abs[:period]) / period

        if avgL == 0:
            rsi[period] = 100.0
        else:
            rsi[period] = 100.0 - 100.0 / (1.0 + avgP / avgL)

        # Wilder smoothing
        for i in range(period, len(d)):
            avgP = (avgP * (period - 1) + p[i]) / period
            avgL = (avgL * (period - 1) + n_abs[i]) / period
            close_idx = i + 1
            if avgL == 0:
                rsi[close_idx] = 100.0
            else:
                rsi[close_idx] = 100.0 - 100.0 / (1.0 + avgP / avgL)

        return rsi


# ═══════════════════════════════════════════════════════════
# CANDLE AGGREGATOR (1-min to N-min)
# ═══════════════════════════════════════════════════════════
class CandleAggregator:
    """Aggregates 1-minute candles into N-minute candles."""

    def __init__(self, interval_minutes):
        self.interval = interval_minutes
        self.candles = []  # Completed N-min candles: {timestamp, open, high, low, close}
        self.current = None  # Current building candle
        self.minute_count = 0

    def add_1min_candle(self, candle):
        """
        Add a 1-min candle. Returns completed N-min candle if interval boundary hit, else None.
        candle: {timestamp, open, high, low, close}
        """
        if self.interval == 1:
            self.candles.append(candle)
            return candle

        ts = candle["timestamp"]
        dt = datetime.fromtimestamp(ts, tz=IST)
        # Calculate minute-of-day for interval alignment
        minutes_since_open = (dt.hour * 60 + dt.minute) - (9 * 60 + 15)  # 09:15 base
        if minutes_since_open < 0:
            minutes_since_open = 0
        bucket = minutes_since_open // self.interval

        if self.current is None:
            self.current = {
                "timestamp": ts,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "bucket": bucket,
            }
            return None

        if bucket != self.current.get("bucket"):
            # Complete the current candle
            completed = {
                "timestamp": self.current["timestamp"],
                "open": self.current["open"],
                "high": self.current["high"],
                "low": self.current["low"],
                "close": self.current["close"],
            }
            self.candles.append(completed)
            # Start new candle
            self.current = {
                "timestamp": ts,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "bucket": bucket,
            }
            return completed
        else:
            # Update current candle
            self.current["high"] = max(self.current["high"], candle["high"])
            self.current["low"] = min(self.current["low"], candle["low"])
            self.current["close"] = candle["close"]
            return None

    def get_current_building(self):
        """Returns the current incomplete candle, or None."""
        if self.current:
            return {
                "timestamp": self.current["timestamp"],
                "open": self.current["open"],
                "high": self.current["high"],
                "low": self.current["low"],
                "close": self.current["close"],
            }
        return None

    def build_from_history(self, candles_1min):
        """Build N-min candles from historical 1-min data."""
        completed_list = []
        for c in candles_1min:
            result = self.add_1min_candle(c)
            if result:
                completed_list.append(result)
        return completed_list


# ═══════════════════════════════════════════════════════════
# STRATEGY ENGINE
# ═══════════════════════════════════════════════════════════
@dataclass
class AlertCandle:
    """Stores alert candle data."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    rsi: float
    signal_type: str  # 'CE_SELL' or 'PE_SELL'


@dataclass
class ActiveTrade:
    """Stores active trade data."""
    trade_id: int
    signal_type: str  # 'CE_SELL' or 'PE_SELL'
    option_type: str  # 'CE' or 'PE'
    security_id: str
    strike: float
    entry_price: float  # Option entry price
    entry_time: datetime
    qty: int
    expiry: str
    spot_at_entry: float
    alert_high: float  # For SL calculation
    alert_low: float
    sl_price: float  # On option price (mapped)
    target_type: str = ""  # 'BB' or 'UNIVERSAL'
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
    is_open: bool = True
    current_ltp: float = 0.0
    order_id_entry: str = ""
    order_id_exit: str = ""
    trailing_sl: float = 0.0  # Current trailing SL on option


class StrategyEngine:
    """
    BB-RSI Option Selling Strategy.

    CE SELL: Alert candle opens ABOVE upper BB, closes inside bands (below upper, above lower).
             Trigger: next candle breaks alert low → SELL CE.
    PE SELL: Alert candle opens BELOW lower BB, closes inside bands (above lower, below upper).
             Trigger: next candle breaks alert high → SELL PE.
    """

    def __init__(self, config):
        self.config = config
        self.indicator = IndicatorEngine()
        self.aggregator = CandleAggregator(config["timeframe_minutes"])
        self.option_aggregator = None  # For option RSI
        self.candle_closes = []  # Underlying close prices for indicator calc
        self.candle_data = []  # Full candle data

        # State
        self.alert_candle: Optional[AlertCandle] = None
        self.waiting_trigger = False
        self.active_trade: Optional[ActiveTrade] = None
        self.trade_history: List[ActiveTrade] = []
        self.trade_count = 0
        self.total_pnl = 0.0

        # Callbacks
        self.on_alert = None  # (alert_candle) -> None
        self.on_trigger = None  # (signal_type) -> None
        self.on_entry = None  # (trade) -> None
        self.on_exit = None  # (trade) -> None
        self.on_log = None  # (message) -> None

        self.lock = threading.Lock()
        self.running = False
        self.option_sec_id = None  # Current option being tracked for RSI

    def _log(self, msg):
        log.info(msg)
        if self.on_log:
            self.on_log(msg)

    def process_completed_candle(self, candle):
        """Process a completed N-minute candle for the underlying."""
        with self.lock:
            self.candle_data.append(candle)
            self.candle_closes.append(candle["close"])

            # Calculate indicators
            bb_period = self.config.get("bb_period", 20)
            bb_mult = self.config.get("bb_multiplier", 2.0)
            rsi_period = self.config.get("rsi_period", 14)

            closes = self.candle_closes
            if len(closes) < max(bb_period, rsi_period + 1):
                return  # Not enough data

            # BB on full history
            bb_upper, bb_mid, bb_lower = self.indicator.calculate_bb(closes, bb_period, bb_mult)
            # RSI on full history
            rsi_vals = self.indicator.calculate_rsi(closes, rsi_period)

            curr_bb_upper = bb_upper[-1]
            curr_bb_mid = bb_mid[-1]
            curr_bb_lower = bb_lower[-1]
            curr_rsi = rsi_vals[-1]

            c_open = candle["open"]
            c_close = candle["close"]
            c_high = candle["high"]
            c_low = candle["low"]

            ts = datetime.fromtimestamp(candle["timestamp"], tz=IST)

            # Check time filter
            no_trade_time = self.config.get("no_trade_after", "14:45")
            h, m = map(int, no_trade_time.split(":"))
            if ts.hour > h or (ts.hour == h and ts.minute >= m):
                if self.waiting_trigger:
                    self._log(f"[{ts.strftime('%H:%M')}] Alert cancelled — past no-trade time")
                    self.alert_candle = None
                    self.waiting_trigger = False
                return

            # If we have an active trade, check exit conditions
            if self.active_trade and self.active_trade.is_open:
                self._check_exit_on_candle(candle, curr_bb_upper, curr_bb_lower, ts)
                return

            # TRIGGER CHECK: if waiting for trigger candle
            if self.waiting_trigger and self.alert_candle:
                alert = self.alert_candle
                if alert.signal_type == "CE_SELL":
                    # Trigger: this candle's low breaks alert candle's low
                    if c_low < alert.low:
                        self._log(f"[{ts.strftime('%H:%M')}] ▼ TRIGGER CE SELL | Low {c_low:.2f} < Alert Low {alert.low:.2f}")
                        self._execute_entry(alert, ts)
                    else:
                        self._log(f"[{ts.strftime('%H:%M')}] ✗ Trigger failed CE | Low {c_low:.2f} >= Alert Low {alert.low:.2f}")
                        self.alert_candle = None
                        self.waiting_trigger = False
                elif alert.signal_type == "PE_SELL":
                    # Trigger: this candle's high breaks alert candle's high
                    if c_high > alert.high:
                        self._log(f"[{ts.strftime('%H:%M')}] ▲ TRIGGER PE SELL | High {c_high:.2f} > Alert High {alert.high:.2f}")
                        self._execute_entry(alert, ts)
                    else:
                        self._log(f"[{ts.strftime('%H:%M')}] ✗ Trigger failed PE | High {c_high:.2f} <= Alert High {alert.high:.2f}")
                        self.alert_candle = None
                        self.waiting_trigger = False
                return

            # ALERT CHECK: Look for alert candle
            if math.isnan(curr_bb_upper) or math.isnan(curr_bb_lower):
                return

            # CE SELL Alert: opens above upper BB, closes inside bands
            if c_open > curr_bb_upper and curr_bb_lower < c_close < curr_bb_upper:
                self._log(f"[{ts.strftime('%H:%M')}] ★ ALERT CE SELL | Open {c_open:.2f} > UBB {curr_bb_upper:.2f} | Close {c_close:.2f} inside bands")
                self.alert_candle = AlertCandle(
                    timestamp=candle["timestamp"], open=c_open, high=c_high, low=c_low, close=c_close,
                    bb_upper=curr_bb_upper, bb_mid=curr_bb_mid, bb_lower=curr_bb_lower,
                    rsi=curr_rsi, signal_type="CE_SELL"
                )
                self.waiting_trigger = True
                if self.on_alert:
                    self.on_alert(self.alert_candle)
                return

            # PE SELL Alert: opens below lower BB, closes inside bands
            if c_open < curr_bb_lower and curr_bb_lower < c_close < curr_bb_upper:
                self._log(f"[{ts.strftime('%H:%M')}] ★ ALERT PE SELL | Open {c_open:.2f} < LBB {curr_bb_lower:.2f} | Close {c_close:.2f} inside bands")
                self.alert_candle = AlertCandle(
                    timestamp=candle["timestamp"], open=c_open, high=c_high, low=c_low, close=c_close,
                    bb_upper=curr_bb_upper, bb_mid=curr_bb_mid, bb_lower=curr_bb_lower,
                    rsi=curr_rsi, signal_type="PE_SELL"
                )
                self.waiting_trigger = True
                if self.on_alert:
                    self.on_alert(self.alert_candle)
                return

    def _check_option_rsi(self, option_sec_id, option_type):
        """
        Check RSI on the option strike.
        For both CE and PE: RSI should be above the threshold (default 70).
        Returns True if RSI filter passes or is disabled.
        """
        if not self.config.get("rsi_filter_enabled", True):
            return True

        threshold = self.config.get("rsi_threshold", 70)
        # Fetch option 1-min data and aggregate to our timeframe
        candles_1min = fetch_option_1min(option_sec_id, days=2)
        if not candles_1min or len(candles_1min) < 20:
            self._log(f"  RSI check: insufficient option data ({len(candles_1min) if candles_1min else 0} candles)")
            return True  # Pass if we can't check

        # Aggregate to our timeframe
        opt_agg = CandleAggregator(self.config["timeframe_minutes"])
        opt_agg.build_from_history(candles_1min)
        opt_closes = [c["close"] for c in opt_agg.candles]

        if len(opt_closes) < 15:
            self._log(f"  RSI check: insufficient aggregated data ({len(opt_closes)} candles)")
            return True

        rsi_vals = self.indicator.calculate_rsi(opt_closes, 14)
        latest_rsi = rsi_vals[-1] if rsi_vals else float('nan')

        if math.isnan(latest_rsi):
            self._log(f"  RSI check: NaN, passing")
            return True

        self._log(f"  Option RSI = {latest_rsi:.2f} | Threshold = {threshold}")

        if latest_rsi >= threshold:
            return True
        else:
            self._log(f"  RSI FILTER BLOCKED: {latest_rsi:.2f} < {threshold}")
            return False

    def _execute_entry(self, alert: AlertCandle, ts: datetime):
        """Execute trade entry."""
        config = self.config
        idx_name = config["index"]
        option_type = "CE" if alert.signal_type == "CE_SELL" else "PE"
        strike_offset = config.get("strike_offset", 0)
        expiry_choice = config.get("expiry", "current")
        lots = config.get("lots", 1)
        mode = config.get("trade_mode", "paper")
        buffer_pts = config.get("sl_buffer", 5)
        max_sl = config.get("max_sl", 50)

        # Resolve option strike
        opt = resolve_option_strike(idx_name, option_type, strike_offset, expiry_choice)
        if not opt:
            self._log(f"  ENTRY FAILED: Could not resolve {idx_name} {option_type} strike")
            self.alert_candle = None
            self.waiting_trigger = False
            return

        # RSI filter check on option
        if not self._check_option_rsi(opt["security_id"], option_type):
            self._log(f"  ENTRY BLOCKED by RSI filter on {option_type} option")
            self.alert_candle = None
            self.waiting_trigger = False
            return

        lot_size = INDEX_MAP[idx_name]["lot_size"]
        qty = lot_size * lots
        entry_price = opt["last_price"]

        # Calculate SL on underlying
        if alert.signal_type == "CE_SELL":
            sl_underlying = alert.high + buffer_pts
        else:
            sl_underlying = alert.low - buffer_pts

        # Cap SL at max_sl points from current spot
        spot = opt["spot"]
        sl_distance = abs(sl_underlying - spot)
        if sl_distance > max_sl:
            if alert.signal_type == "CE_SELL":
                sl_underlying = spot + max_sl
            else:
                sl_underlying = spot - max_sl
            self._log(f"  SL capped to {max_sl} pts | SL={sl_underlying:.2f}")

        # Place order (SELL for selling options)
        order_id = ""
        if mode == "live":
            oid, fp = place_order(opt["security_id"], "NSE_FNO", qty, "SELL")
            if not oid:
                self._log(f"  ENTRY ORDER FAILED")
                self.alert_candle = None
                self.waiting_trigger = False
                return
            order_id = oid
            if fp > 0:
                entry_price = fp

        self.trade_count += 1
        trade = ActiveTrade(
            trade_id=self.trade_count,
            signal_type=alert.signal_type,
            option_type=option_type,
            security_id=opt["security_id"],
            strike=opt["strike"],
            entry_price=entry_price,
            entry_time=ts,
            qty=qty,
            expiry=opt["expiry"],
            spot_at_entry=spot,
            alert_high=alert.high,
            alert_low=alert.low,
            sl_price=entry_price * 1.5,  # Initial SL mapped to option (will refine with LTP)
            current_ltp=entry_price,
            order_id_entry=order_id,
            trailing_sl=0.0,
        )
        self.active_trade = trade
        self.alert_candle = None
        self.waiting_trigger = False

        self._log(f"  ✓ ENTRY #{self.trade_count} | SELL {option_type} {int(opt['strike'])} "
                  f"| Price={entry_price:.2f} | Qty={qty} | SL(underlying)={sl_underlying:.2f} | {mode.upper()}")

        if self.on_entry:
            self.on_entry(trade)

    def _check_exit_on_candle(self, candle, bb_upper, bb_lower, ts):
        """Check exit conditions on a completed candle."""
        trade = self.active_trade
        if not trade or not trade.is_open:
            return

        config = self.config
        c_close = candle["close"]
        c_low = candle["low"]
        c_high = candle["high"]
        spot = c_close  # Current underlying price

        # 1. BB Target for CE SELL: candle close or low below lower BB
        if trade.signal_type == "CE_SELL":
            if not math.isnan(bb_lower) and (c_close < bb_lower or c_low < bb_lower):
                self._log(f"[{ts.strftime('%H:%M')}] ✓ BB TARGET HIT (CE) | Close/Low below LBB {bb_lower:.2f}")
                self._execute_exit(trade, "BB_TARGET", ts)
                return

        # 2. BB Target for PE SELL: candle close or high above upper BB
        if trade.signal_type == "PE_SELL":
            if not math.isnan(bb_upper) and (c_close > bb_upper or c_high > bb_upper):
                self._log(f"[{ts.strftime('%H:%M')}] ✓ BB TARGET HIT (PE) | Close/High above UBB {bb_upper:.2f}")
                self._execute_exit(trade, "BB_TARGET", ts)
                return

        # 3. SL check on underlying
        buffer_pts = config.get("sl_buffer", 5)
        max_sl = config.get("max_sl", 50)
        if trade.signal_type == "CE_SELL":
            sl_level = min(trade.alert_high + buffer_pts, trade.spot_at_entry + max_sl)
            if c_high >= sl_level:
                self._log(f"[{ts.strftime('%H:%M')}] ✗ SL HIT (CE) | High {c_high:.2f} >= SL {sl_level:.2f}")
                self._execute_exit(trade, "SL", ts)
                return
        else:
            sl_level = max(trade.alert_low - buffer_pts, trade.spot_at_entry - max_sl)
            if c_low <= sl_level:
                self._log(f"[{ts.strftime('%H:%M')}] ✗ SL HIT (PE) | Low {c_low:.2f} <= SL {sl_level:.2f}")
                self._execute_exit(trade, "SL", ts)
                return

        # 4. Universal target on option P&L
        universal_target = config.get("universal_target", 70)
        if trade.current_ltp > 0:
            # For sold options, profit = entry - current
            option_pnl_per_unit = trade.entry_price - trade.current_ltp
            if option_pnl_per_unit >= universal_target:
                self._log(f"[{ts.strftime('%H:%M')}] ✓ UNIVERSAL TARGET | PnL/unit={option_pnl_per_unit:.2f} >= {universal_target}")
                self._execute_exit(trade, "UNIVERSAL_TARGET", ts)
                return

        # 5. Profit trailing
        if config.get("trailing_enabled", False) and trade.current_ltp > 0:
            self._check_trailing(trade, ts)

    def _check_trailing(self, trade: ActiveTrade, ts: datetime):
        """Multi-step profit trailing on option price."""
        config = self.config
        trail_step = config.get("trail_step", 10)
        trail_lock = config.get("trail_lock", 6)

        # For sold option: profit = entry_price - current_ltp
        profit = trade.entry_price - trade.current_ltp
        if profit <= 0:
            return

        # Calculate how many steps of profit we've hit
        steps = int(profit // trail_step)
        if steps < 1:
            return

        # Multi-step: at 10 profit lock 6, at 20 lock 16, at 30 lock 26, etc.
        # Trail SL = entry_price - (steps * trail_step - (trail_step - trail_lock))
        # i.e., lock profit = steps * trail_step - (trail_step - trail_lock)
        locked_profit = steps * trail_step - (trail_step - trail_lock)
        new_trailing_sl = trade.entry_price - locked_profit  # SL on option price

        if trade.trailing_sl == 0 or new_trailing_sl < trade.trailing_sl:
            trade.trailing_sl = new_trailing_sl
            self._log(f"[{ts.strftime('%H:%M')}] ↕ TRAIL | Profit={profit:.2f} | Steps={steps} | SL moved to {new_trailing_sl:.2f}")

        # Check if trailing SL hit (option price went above our trailing SL)
        if trade.trailing_sl > 0 and trade.current_ltp >= trade.trailing_sl:
            self._log(f"[{ts.strftime('%H:%M')}] ✓ TRAILING SL HIT | LTP={trade.current_ltp:.2f} >= Trail SL={trade.trailing_sl:.2f}")
            self._execute_exit(trade, "TRAILING_SL", ts)

    def _execute_exit(self, trade: ActiveTrade, reason: str, ts: datetime):
        """Execute trade exit."""
        config = self.config
        mode = config.get("trade_mode", "paper")
        exit_price = trade.current_ltp if trade.current_ltp > 0 else trade.entry_price

        if mode == "live":
            oid, fp = place_order(trade.security_id, "NSE_FNO", trade.qty, "BUY")
            if oid:
                trade.order_id_exit = oid
                if fp > 0:
                    exit_price = fp
            else:
                self._log(f"  EXIT ORDER FAILED — manual exit needed!")

        # For sold options: PnL = (entry - exit) * qty
        pnl_per = trade.entry_price - exit_price
        trade.exit_price = exit_price
        trade.exit_time = ts
        trade.pnl = pnl_per * trade.qty
        trade.is_open = False
        trade.target_type = reason

        self.total_pnl += trade.pnl
        self.trade_history.append(trade)
        self.active_trade = None

        self._log(f"  ✓ EXIT #{trade.trade_id} | {reason} | {trade.option_type} {int(trade.strike)} "
                  f"| {trade.entry_price:.2f} → {exit_price:.2f} | PnL={trade.pnl:+.2f} | Total={self.total_pnl:+.2f}")

        if self.on_exit:
            self.on_exit(trade)

    def force_exit(self, reason="FORCE_EXIT"):
        """Force exit any open trade."""
        with self.lock:
            if self.active_trade and self.active_trade.is_open:
                self._execute_exit(self.active_trade, reason, now_ist())

    def update_option_ltp(self, ltp):
        """Update current LTP of the traded option."""
        with self.lock:
            if self.active_trade and self.active_trade.is_open:
                self.active_trade.current_ltp = ltp


# ═══════════════════════════════════════════════════════════
# LIVE POLLING ENGINE
# ═══════════════════════════════════════════════════════════
class LivePollingEngine:
    """
    Polls Dhan REST API for 1-min candles and feeds them to StrategyEngine.
    Also polls option LTP when a trade is active.
    """

    def __init__(self, strategy: StrategyEngine, config: dict):
        self.strategy = strategy
        self.config = config
        self.stop_event = threading.Event()
        self.last_candle_ts = 0
        self.last_option_poll_ts = 0

    def run(self):
        """Main polling loop."""
        idx_name = self.config["index"]
        idx_info = INDEX_MAP[idx_name]
        sid = idx_info["security_id"]
        segment = idx_info["segment"]
        poll_interval = 5  # seconds

        log.info(f"Polling started for {idx_name} | TF={self.config['timeframe_minutes']}min")

        # Load historical data first
        candles_1min = fetch_historical_1min(sid, segment, days=5)
        if candles_1min:
            log.info(f"Building from {len(candles_1min)} historical 1-min candles...")
            for c in candles_1min:
                completed = self.strategy.aggregator.add_1min_candle(c)
                if completed:
                    self.strategy.process_completed_candle(completed)
            self.last_candle_ts = candles_1min[-1]["timestamp"] if candles_1min else 0
            log.info(f"Historical load done. {len(self.strategy.candle_data)} candles built.")

        while not self.stop_event.is_set():
            try:
                n = now_ist()

                # Force exit check
                fe_time = self.config.get("force_exit_time", "15:05")
                feh, fem = map(int, fe_time.split(":"))
                if n.hour > feh or (n.hour == feh and n.minute >= fem):
                    if self.strategy.active_trade and self.strategy.active_trade.is_open:
                        log.info(f"FORCE EXIT at {n.strftime('%H:%M:%S')}")
                        self.strategy.force_exit("TIME_EXIT")

                # Only poll during market hours (09:15 to 15:30)
                if n.hour < 9 or (n.hour == 9 and n.minute < 15):
                    self.stop_event.wait(timeout=10)
                    continue
                if n.hour > 15 or (n.hour == 15 and n.minute > 30):
                    self.stop_event.wait(timeout=10)
                    continue

                # Fetch latest 1-min candles
                candles = fetch_historical_1min(sid, segment, days=1)
                if candles:
                    new_count = 0
                    for c in candles:
                        if c["timestamp"] > self.last_candle_ts:
                            completed = self.strategy.aggregator.add_1min_candle(c)
                            if completed:
                                self.strategy.process_completed_candle(completed)
                                new_count += 1
                            self.last_candle_ts = c["timestamp"]
                    if new_count > 0:
                        log.info(f"Processed {new_count} new candle(s)")

                # Poll option LTP if trade is active
                if self.strategy.active_trade and self.strategy.active_trade.is_open:
                    self._poll_option_ltp()

            except Exception as e:
                log.error(f"Poll error: {e}")

            self.stop_event.wait(timeout=poll_interval)

    def _poll_option_ltp(self):
        """Poll the current option LTP via charts API."""
        trade = self.strategy.active_trade
        if not trade:
            return
        candles = fetch_historical_data_generic(trade.security_id, "NSE_FNO", "OPTIDX", days=1)
        if candles and len(candles) > 0:
            ltp = candles[-1]["close"]
            self.strategy.update_option_ltp(ltp)

    def stop(self):
        self.stop_event.set()


# ═══════════════════════════════════════════════════════════
# MODERN UI — CustomTkinter
# ═══════════════════════════════════════════════════════════
class BBRSIApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Balfund BB-RSI Option Seller v1.0")
        self.geometry("1100x820")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.strategy: Optional[StrategyEngine] = None
        self.poller: Optional[LivePollingEngine] = None
        self.poll_thread: Optional[threading.Thread] = None
        self.connected = False
        self.log_buffer = deque(maxlen=200)

        self._build_ui()
        self._load_env_values()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # ── Main grid
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ══════════ HEADER ══════════
        header = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=0, height=60)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(header, text="⚡ BALFUND", font=("Segoe UI", 20, "bold"),
                     text_color="#00d4ff").grid(row=0, column=0, padx=15, pady=10)
        ctk.CTkLabel(header, text="BB-RSI Option Seller", font=("Segoe UI", 14),
                     text_color="#8892b0").grid(row=0, column=1, padx=5, pady=10, sticky="w")

        self.status_label = ctk.CTkLabel(header, text="● Disconnected", font=("Segoe UI", 12),
                                         text_color="#ff4444")
        self.status_label.grid(row=0, column=2, padx=15, pady=10)

        # ══════════ CONFIG PANEL ══════════
        config_frame = ctk.CTkFrame(self, fg_color="#16213e")
        config_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(5, 0))

        # Row 1: Connection
        conn_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        conn_frame.pack(fill="x", padx=10, pady=(8, 4))

        ctk.CTkLabel(conn_frame, text="Client ID:", font=("Segoe UI", 11)).pack(side="left", padx=(0, 5))
        self.client_id_entry = ctk.CTkEntry(conn_frame, width=120, placeholder_text="Client ID")
        self.client_id_entry.pack(side="left", padx=2)

        ctk.CTkLabel(conn_frame, text="PIN:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.pin_entry = ctk.CTkEntry(conn_frame, width=80, show="*", placeholder_text="PIN")
        self.pin_entry.pack(side="left", padx=2)

        ctk.CTkLabel(conn_frame, text="TOTP Secret:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.totp_entry = ctk.CTkEntry(conn_frame, width=200, show="*", placeholder_text="TOTP Secret")
        self.totp_entry.pack(side="left", padx=2)

        self.connect_btn = ctk.CTkButton(conn_frame, text="Connect", width=100,
                                         fg_color="#0f3460", hover_color="#1a5276",
                                         command=self._connect)
        self.connect_btn.pack(side="left", padx=(15, 0))

        # Row 2: Strategy params
        params_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        params_frame.pack(fill="x", padx=10, pady=4)

        ctk.CTkLabel(params_frame, text="Index:", font=("Segoe UI", 11)).pack(side="left", padx=(0, 5))
        self.index_var = ctk.StringVar(value="NIFTY")
        self.index_menu = ctk.CTkOptionMenu(params_frame, variable=self.index_var,
                                            values=["NIFTY", "BANKNIFTY"], width=120)
        self.index_menu.pack(side="left", padx=2)

        ctk.CTkLabel(params_frame, text="Timeframe:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.tf_var = ctk.StringVar(value="3 Minutes")
        self.tf_menu = ctk.CTkOptionMenu(params_frame, variable=self.tf_var,
                                         values=list(TIMEFRAME_MAP.keys()), width=120)
        self.tf_menu.pack(side="left", padx=2)

        ctk.CTkLabel(params_frame, text="Strike Offset:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.offset_var = ctk.StringVar(value="0")
        self.offset_entry = ctk.CTkEntry(params_frame, width=50, textvariable=self.offset_var)
        self.offset_entry.pack(side="left", padx=2)

        ctk.CTkLabel(params_frame, text="Expiry:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.expiry_var = ctk.StringVar(value="current")
        self.expiry_menu = ctk.CTkOptionMenu(params_frame, variable=self.expiry_var,
                                             values=["current", "next"], width=100)
        self.expiry_menu.pack(side="left", padx=2)

        ctk.CTkLabel(params_frame, text="Lots:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.lots_var = ctk.StringVar(value="1")
        self.lots_entry = ctk.CTkEntry(params_frame, width=40, textvariable=self.lots_var)
        self.lots_entry.pack(side="left", padx=2)

        ctk.CTkLabel(params_frame, text="Mode:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.mode_var = ctk.StringVar(value="paper")
        self.mode_menu = ctk.CTkOptionMenu(params_frame, variable=self.mode_var,
                                           values=["paper", "live"], width=80)
        self.mode_menu.pack(side="left", padx=2)

        # Row 3: BB/RSI/SL/Target params
        params2_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        params2_frame.pack(fill="x", padx=10, pady=4)

        ctk.CTkLabel(params2_frame, text="BB Period:", font=("Segoe UI", 11)).pack(side="left", padx=(0, 5))
        self.bb_period_var = ctk.StringVar(value="20")
        ctk.CTkEntry(params2_frame, width=40, textvariable=self.bb_period_var).pack(side="left", padx=2)

        ctk.CTkLabel(params2_frame, text="BB Mult:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.bb_mult_var = ctk.StringVar(value="2.0")
        ctk.CTkEntry(params2_frame, width=40, textvariable=self.bb_mult_var).pack(side="left", padx=2)

        ctk.CTkLabel(params2_frame, text="SL Buffer:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.sl_buffer_var = ctk.StringVar(value="5")
        ctk.CTkEntry(params2_frame, width=40, textvariable=self.sl_buffer_var).pack(side="left", padx=2)

        ctk.CTkLabel(params2_frame, text="Max SL:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.max_sl_var = ctk.StringVar(value="50")
        ctk.CTkEntry(params2_frame, width=40, textvariable=self.max_sl_var).pack(side="left", padx=2)

        ctk.CTkLabel(params2_frame, text="Uni Target:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.target_var = ctk.StringVar(value="70")
        ctk.CTkEntry(params2_frame, width=40, textvariable=self.target_var).pack(side="left", padx=2)

        ctk.CTkLabel(params2_frame, text="No Trade:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.no_trade_var = ctk.StringVar(value="14:45")
        ctk.CTkEntry(params2_frame, width=55, textvariable=self.no_trade_var).pack(side="left", padx=2)

        ctk.CTkLabel(params2_frame, text="Force Exit:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.force_exit_var = ctk.StringVar(value="15:05")
        ctk.CTkEntry(params2_frame, width=55, textvariable=self.force_exit_var).pack(side="left", padx=2)

        # Row 4: RSI filter + Trailing
        params3_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        params3_frame.pack(fill="x", padx=10, pady=(4, 8))

        self.rsi_enabled_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(params3_frame, text="RSI Filter", variable=self.rsi_enabled_var,
                        font=("Segoe UI", 11)).pack(side="left", padx=(0, 5))

        ctk.CTkLabel(params3_frame, text="RSI ≥", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.rsi_thresh_var = ctk.StringVar(value="70")
        ctk.CTkEntry(params3_frame, width=40, textvariable=self.rsi_thresh_var).pack(side="left", padx=2)

        self.trail_enabled_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(params3_frame, text="Profit Trailing", variable=self.trail_enabled_var,
                        font=("Segoe UI", 11)).pack(side="left", padx=(20, 5))

        ctk.CTkLabel(params3_frame, text="Step:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.trail_step_var = ctk.StringVar(value="10")
        ctk.CTkEntry(params3_frame, width=40, textvariable=self.trail_step_var).pack(side="left", padx=2)

        ctk.CTkLabel(params3_frame, text="Lock:", font=("Segoe UI", 11)).pack(side="left", padx=(10, 5))
        self.trail_lock_var = ctk.StringVar(value="6")
        ctk.CTkEntry(params3_frame, width=40, textvariable=self.trail_lock_var).pack(side="left", padx=2)

        # Start/Stop buttons
        self.start_btn = ctk.CTkButton(params3_frame, text="▶ START", width=100,
                                       fg_color="#00875a", hover_color="#00a86b",
                                       command=self._start_strategy, state="disabled")
        self.start_btn.pack(side="left", padx=(30, 5))

        self.stop_btn = ctk.CTkButton(params3_frame, text="■ STOP", width=100,
                                      fg_color="#c0392b", hover_color="#e74c3c",
                                      command=self._stop_strategy, state="disabled")
        self.stop_btn.pack(side="left", padx=5)

        self.exit_btn = ctk.CTkButton(params3_frame, text="⚡ EXIT TRADE", width=110,
                                      fg_color="#e67e22", hover_color="#f39c12",
                                      command=self._force_exit_trade, state="disabled")
        self.exit_btn.pack(side="left", padx=5)

        # ══════════ BOTTOM AREA: Trade Panel + Log ══════════
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_columnconfigure(1, weight=2)
        bottom.grid_rowconfigure(0, weight=1)

        # Trade info panel
        trade_frame = ctk.CTkFrame(bottom, fg_color="#16213e")
        trade_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        ctk.CTkLabel(trade_frame, text="TRADE STATUS", font=("Segoe UI", 13, "bold"),
                     text_color="#00d4ff").pack(pady=(10, 5))

        self.trade_info_text = ctk.CTkTextbox(trade_frame, font=("Consolas", 11),
                                              fg_color="#0a0a1a", text_color="#c0c0c0",
                                              state="disabled", wrap="word")
        self.trade_info_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Log panel
        log_frame = ctk.CTkFrame(bottom, fg_color="#16213e")
        log_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        ctk.CTkLabel(log_frame, text="STRATEGY LOG", font=("Segoe UI", 13, "bold"),
                     text_color="#00d4ff").pack(pady=(10, 5))

        self.log_text = ctk.CTkTextbox(log_frame, font=("Consolas", 10),
                                       fg_color="#0a0a1a", text_color="#a0a0a0",
                                       state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # ══════════ FOOTER ══════════
        footer = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=0, height=30)
        footer.grid(row=3, column=0, sticky="ew")
        ctk.CTkLabel(footer, text="© Balfund Trading Pvt Ltd | www.balfund.com | info@balfund.com",
                     font=("Segoe UI", 10), text_color="#555").pack(pady=5)

        # Start dashboard refresh timer
        self._refresh_dashboard()

    def _load_env_values(self):
        cid = os.getenv("DHAN_CLIENT_ID", "")
        pin = os.getenv("DHAN_PIN", "")
        totp = os.getenv("DHAN_TOTP_SECRET", "")
        if cid:
            self.client_id_entry.insert(0, cid)
        if pin:
            self.pin_entry.insert(0, pin)
        if totp:
            self.totp_entry.insert(0, totp)

    def _connect(self):
        cid = self.client_id_entry.get().strip()
        pin = self.pin_entry.get().strip()
        totp = self.totp_entry.get().strip()

        if not all([cid, pin, totp]):
            self._append_log("ERROR: Fill in Client ID, PIN, and TOTP Secret")
            return

        # Save to .env
        set_key(str(ENV_FILE), "DHAN_CLIENT_ID", cid)
        set_key(str(ENV_FILE), "DHAN_PIN", pin)
        set_key(str(ENV_FILE), "DHAN_TOTP_SECRET", totp)
        os.environ["DHAN_CLIENT_ID"] = cid
        os.environ["DHAN_PIN"] = pin
        os.environ["DHAN_TOTP_SECRET"] = totp

        self.connect_btn.configure(state="disabled", text="Connecting...")
        self._append_log("Connecting to Dhan...")

        def _do_connect():
            ok, msg = init_credentials()
            self.after(0, lambda: self._on_connect_result(ok, msg))

        threading.Thread(target=_do_connect, daemon=True).start()

    def _on_connect_result(self, ok, msg):
        if ok:
            self.connected = True
            self.status_label.configure(text="● Connected", text_color="#00e676")
            self.connect_btn.configure(text="Connected ✓", fg_color="#00875a")
            self.start_btn.configure(state="normal")
            self._append_log(f"Connected to Dhan successfully")
        else:
            self.connected = False
            self.status_label.configure(text="● Failed", text_color="#ff4444")
            self.connect_btn.configure(state="normal", text="Connect")
            self._append_log(f"Connection failed: {msg}")

    def _get_config(self):
        tf_key = self.tf_var.get()
        _, tf_minutes = TIMEFRAME_MAP.get(tf_key, ("1", 3))
        return {
            "index": self.index_var.get(),
            "timeframe_minutes": tf_minutes,
            "strike_offset": int(self.offset_var.get() or 0),
            "expiry": self.expiry_var.get(),
            "lots": int(self.lots_var.get() or 1),
            "trade_mode": self.mode_var.get(),
            "bb_period": int(self.bb_period_var.get() or 20),
            "bb_multiplier": float(self.bb_mult_var.get() or 2.0),
            "sl_buffer": int(self.sl_buffer_var.get() or 5),
            "max_sl": int(self.max_sl_var.get() or 50),
            "universal_target": int(self.target_var.get() or 70),
            "no_trade_after": self.no_trade_var.get() or "14:45",
            "force_exit_time": self.force_exit_var.get() or "15:05",
            "rsi_filter_enabled": self.rsi_enabled_var.get(),
            "rsi_threshold": int(self.rsi_thresh_var.get() or 70),
            "trailing_enabled": self.trail_enabled_var.get(),
            "trail_step": int(self.trail_step_var.get() or 10),
            "trail_lock": int(self.trail_lock_var.get() or 6),
            "rsi_period": 14,
        }

    def _start_strategy(self):
        if not self.connected:
            self._append_log("ERROR: Connect to Dhan first")
            return

        config = self._get_config()
        self._append_log(f"Starting strategy: {config['index']} | TF={config['timeframe_minutes']}min | "
                         f"Mode={config['trade_mode']} | Lots={config['lots']}")

        self.strategy = StrategyEngine(config)
        self.strategy.on_log = lambda msg: self.after(0, lambda m=msg: self._append_log(m))
        self.strategy.on_alert = lambda a: self.after(0, lambda: self._update_trade_info())
        self.strategy.on_entry = lambda t: self.after(0, lambda: self._update_trade_info())
        self.strategy.on_exit = lambda t: self.after(0, lambda: self._update_trade_info())

        self.poller = LivePollingEngine(self.strategy, config)
        self.poll_thread = threading.Thread(target=self.poller.run, daemon=True)
        self.poll_thread.start()

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.exit_btn.configure(state="normal")
        self._append_log("Strategy running...")

    def _stop_strategy(self):
        if self.poller:
            self.poller.stop()
        if self.strategy and self.strategy.active_trade and self.strategy.active_trade.is_open:
            self.strategy.force_exit("MANUAL_STOP")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.exit_btn.configure(state="disabled")
        self._append_log("Strategy stopped.")

    def _force_exit_trade(self):
        if self.strategy:
            self.strategy.force_exit("MANUAL_EXIT")
            self._update_trade_info()

    def _append_log(self, msg):
        ts = now_ist().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_buffer.append(line)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _update_trade_info(self):
        if not self.strategy:
            return
        s = self.strategy
        lines = []
        lines.append(f"{'='*40}")
        lines.append(f"  Index: {self.config_get('index', 'NIFTY')}")
        lines.append(f"  Candles: {len(s.candle_data)}")
        lines.append(f"  Trades: {s.trade_count}")
        lines.append(f"  Total PnL: {s.total_pnl:+.2f}")
        lines.append(f"{'='*40}")

        if s.waiting_trigger and s.alert_candle:
            a = s.alert_candle
            lines.append(f"\n  ★ ALERT CANDLE ({a.signal_type})")
            lines.append(f"  O={a.open:.2f} H={a.high:.2f}")
            lines.append(f"  L={a.low:.2f} C={a.close:.2f}")
            lines.append(f"  BB: {a.bb_lower:.2f} / {a.bb_mid:.2f} / {a.bb_upper:.2f}")
            lines.append(f"  Waiting for trigger...")

        if s.active_trade and s.active_trade.is_open:
            t = s.active_trade
            pnl = (t.entry_price - t.current_ltp) * t.qty if t.current_ltp > 0 else 0
            lines.append(f"\n  ⚡ ACTIVE TRADE #{t.trade_id}")
            lines.append(f"  SELL {t.option_type} {int(t.strike)}")
            lines.append(f"  Entry: {t.entry_price:.2f}")
            lines.append(f"  LTP: {t.current_ltp:.2f}")
            lines.append(f"  PnL: {pnl:+.2f}")
            lines.append(f"  Qty: {t.qty}")
            if t.trailing_sl > 0:
                lines.append(f"  Trail SL: {t.trailing_sl:.2f}")
        elif not s.waiting_trigger:
            lines.append(f"\n  FLAT — Scanning for alerts...")

        if s.trade_history:
            lines.append(f"\n  {'─'*38}")
            lines.append(f"  TRADE HISTORY (last 5):")
            for t in s.trade_history[-5:]:
                d = "SELL" if True else "BUY"
                p = "+" if t.pnl >= 0 else ""
                lines.append(f"  #{t.trade_id} {t.option_type} {int(t.strike)} "
                             f"| {t.entry_price:.2f}→{t.exit_price:.2f} "
                             f"| {p}{t.pnl:.2f} | {t.target_type}")

        self.trade_info_text.configure(state="normal")
        self.trade_info_text.delete("1.0", "end")
        self.trade_info_text.insert("1.0", "\n".join(lines))
        self.trade_info_text.configure(state="disabled")

    def config_get(self, key, default=""):
        try:
            return self._get_config().get(key, default)
        except:
            return default

    def _refresh_dashboard(self):
        """Periodic UI refresh."""
        if self.strategy:
            self._update_trade_info()
        self.after(2000, self._refresh_dashboard)

    def _on_close(self):
        if self.poller:
            self.poller.stop()
        if self.strategy and self.strategy.active_trade and self.strategy.active_trade.is_open:
            self.strategy.force_exit("APP_CLOSE")
        self.destroy()


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    app = BBRSIApp()
    app.mainloop()


if __name__ == "__main__":
    main()
