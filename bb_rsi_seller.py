#!/usr/bin/env python3
"""
Balfund BB-RSI Option Seller v2.0
Bollinger Band + RSI based NIFTY/BANKNIFTY Option Selling Strategy
Dhan API | WebSocket v2 | CustomTkinter Modern UI

Author: Balfund Trading Pvt Ltd (www.balfund.com)
"""

import os, sys, json, time, math, struct, threading, logging, csv, io
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Dict, Optional, List
from pathlib import Path
from collections import deque

# Fix Windows console encoding (stdout/stderr are None in --windowed EXE)
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import requests, pyotp, websocket
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

HEADERS: Dict[str, str] = {}
DHAN_CLIENT_ID = ""
DHAN_ACCESS_TOKEN = ""
WS_URL: str = ""

# WebSocket protocol constants
REQ_SUB_TICKER = 15
REQ_UNSUB_TICKER = 16
RESP_TICKER = 2
RESP_PREV_CLOSE = 6
RESP_DISCONNECT = 50
EXCH_SEG_MAP = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY",
                4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO"}

INDEX_MAP = {
    "NIFTY": {"security_id": "13", "strike_gap": 50, "segment": "IDX_I"},
    "BANKNIFTY": {"security_id": "25", "strike_gap": 100, "segment": "IDX_I"},
}

# Cache for auto-fetched lot sizes
LOT_SIZE_CACHE: Dict[str, int] = {}

TIMEFRAME_MAP = {
    "1 Minute": 1, "3 Minutes": 3, "5 Minutes": 5, "15 Minutes": 15,
    "30 Minutes": 30, "1 Hour": 60, "2 Hours": 120, "4 Hours": 240,
}

# ═══════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, f"bb_rsi_{datetime.now().strftime('%Y%m%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger("BB_RSI")

# ═══════════════════════════════════════════════════════════
# TRADE LOG (CSV)
# ═══════════════════════════════════════════════════════════
TRADE_DIR = "trades"
os.makedirs(TRADE_DIR, exist_ok=True)

class TradeLogger:
    """Auto-generates trade log CSV with entry/exit details."""
    HEADERS = ["Trade#", "Index", "Type", "Strike", "Expiry", "Alert_Time", "Entry_Time",
               "Entry_Price", "Exit_Time", "Exit_Price", "Exit_Reason", "Qty", "Lot_Size", "PnL"]

    def __init__(self):
        self.filepath = os.path.join(TRADE_DIR, f"bb_rsi_trades_{datetime.now().strftime('%Y%m%d')}.csv")
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.HEADERS)
        log.info(f"Trade log: {self.filepath}")

    def log_entry(self, trade_id, index, option_type, strike, expiry, alert_time, entry_time, entry_price, qty, lot_size):
        with open(self.filepath, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([trade_id, index, f"SELL {option_type}", int(strike), expiry,
                                    alert_time, entry_time, f"{entry_price:.2f}", "", "", "", qty, lot_size, ""])

    def log_exit(self, trade_id, exit_time, exit_price, exit_reason, pnl):
        rows = []
        with open(self.filepath, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        for i, row in enumerate(rows):
            if row and row[0] == str(trade_id):
                rows[i][8] = exit_time
                rows[i][9] = f"{exit_price:.2f}"
                rows[i][10] = exit_reason
                rows[i][13] = f"{pnl:.2f}"
                break
        with open(self.filepath, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

trade_logger = TradeLogger()

# ═══════════════════════════════════════════════════════════
# ENV FILE
# ═══════════════════════════════════════════════════════════
BASE_DIR = Path(os.path.dirname(sys.executable)) if getattr(sys, 'frozen', False) else Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
if not ENV_FILE.exists():
    ENV_FILE.write_text("DHAN_CLIENT_ID=\nDHAN_PIN=\nDHAN_TOTP_SECRET=\nDHAN_ACCESS_TOKEN=\n")
load_dotenv(str(ENV_FILE), override=True)

# ═══════════════════════════════════════════════════════════
# RATE LIMITER
# ═══════════════════════════════════════════════════════════
_api_call_times = deque(maxlen=5)
_api_lock = threading.Lock()

def _rate_limit():
    with _api_lock:
        now = time.time()
        if len(_api_call_times) >= 5:
            elapsed = now - _api_call_times[0]
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed + 0.05)
        _api_call_times.append(time.time())


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
        if not token: return False
        try:
            r = requests.get(AUTH_VERIFY_URL, headers={"access-token": token, "client-id": self.client_id}, timeout=10)
            return r.status_code == 200
        except: return False

    def renew(self, token):
        try:
            d = requests.get(AUTH_RENEW_URL, headers={"access-token": token, "dhanClientId": self.client_id, "Content-Type": "application/json"}, timeout=15).json()
            if "accessToken" in d: log.info("Token renewed"); return d["accessToken"]
        except: pass
        return None

    def generate(self, max_retries=3):
        for attempt in range(max_retries):
            rem = 30 - (int(time.time()) % 30)
            if attempt > 0 or rem < 10:
                log.info(f"Waiting {rem+1}s for TOTP..."); time.sleep(rem + 1)
            totp = pyotp.TOTP(self.totp_secret).now()
            log.info(f"Attempt {attempt+1}: TOTP={totp}")
            try:
                d = requests.post(AUTH_GENERATE_URL, params={"dhanClientId": self.client_id, "pin": self.pin, "totp": totp}, timeout=15).json()
                if "accessToken" in d: log.info("Token generated"); return d["accessToken"]
                err = str(d.get("errorMessage") or d)
                log.warning(f"Generate failed: {err}")
                if "totp" in err.lower(): continue
                return None
            except Exception as e:
                log.warning(f"Generate exception: {e}"); time.sleep(2)
        return None

    def ensure_token(self):
        if self.existing_token:
            log.info("Verifying existing token...")
            if self.verify(self.existing_token): log.info("Token valid"); return self.existing_token
            r = self.renew(self.existing_token)
            if r: self._save(r); return r
        t = self.generate()
        if not t: log.error("Could not obtain token"); return None
        self._save(t); return t

    def _save(self, t):
        try: set_key(str(ENV_FILE), "DHAN_ACCESS_TOKEN", t); log.info("Token saved to .env")
        except: pass


def init_credentials():
    global HEADERS, DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID, WS_URL
    DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
    pin = os.getenv("DHAN_PIN", "").strip()
    totp_secret = os.getenv("DHAN_TOTP_SECRET", "").strip()
    DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()

    if not all([DHAN_CLIENT_ID, pin, totp_secret]):
        return False, "Missing DHAN_CLIENT_ID, DHAN_PIN, or DHAN_TOTP_SECRET in .env"

    log.info("Authenticating...")
    tm = DhanTokenManager(DHAN_CLIENT_ID, pin, totp_secret, DHAN_ACCESS_TOKEN)
    token = tm.ensure_token()
    if not token: return False, "Failed to obtain Dhan access token"
    DHAN_ACCESS_TOKEN = token
    HEADERS.update({"Content-Type": "application/json", "Accept": "application/json",
                    "access-token": token, "client-id": DHAN_CLIENT_ID})
    WS_URL = f"wss://api-feed.dhan.co?version=2&token={token}&clientId={DHAN_CLIENT_ID}&authType=2"
    return True, "Connected"


# ═══════════════════════════════════════════════════════════
# API HELPERS
# ═══════════════════════════════════════════════════════════
def now_ist():
    return datetime.now(IST)

def api_post(ep, payload, retries=2):
    url = f"{BASE_URL}{ep}"
    for a in range(retries + 1):
        _rate_limit()
        try:
            r = requests.post(url, headers=HEADERS, json=payload, timeout=15)
            if r.status_code == 200: return r.json()
            if r.status_code == 429:
                log.warning(f"API {ep} -> 429, waiting 2s..."); time.sleep(2); continue
            log.warning(f"API {ep} -> {r.status_code}: {r.text[:200]}")
        except Exception as e: log.error(f"API {ep} err: {e}")
        if a < retries: time.sleep(1)
    return None

def api_get(ep, retries=2):
    url = f"{BASE_URL}{ep}"
    for a in range(retries + 1):
        _rate_limit()
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200: return r.json()
            if r.status_code == 429:
                log.warning(f"GET {ep} -> 429, waiting 2s..."); time.sleep(2); continue
            log.warning(f"GET {ep} -> {r.status_code}: {r.text[:200]}")
        except Exception as e: log.error(f"GET {ep} err: {e}")
        if a < retries: time.sleep(1)
    return None

def _norm_epoch(ts):
    ts = int(ts)
    d = ts - int(time.time())
    if 16200 <= d <= 23400: ts -= 19800
    return ts

# ═══════════════════════════════════════════════════════════
# WEBSOCKET BINARY PARSER
# ═══════════════════════════════════════════════════════════
def parse_header_8(msg):
    if len(msg) < 8: return None
    return {"resp_code": msg[0], "security_id": str(struct.unpack_from("<I", msg, 4)[0]), "payload": msg[8:]}

def parse_ticker(payload):
    if len(payload) < 8: return None
    return {"ltp": float(struct.unpack_from("<f", payload, 0)[0]), "ltt_epoch": int(struct.unpack_from("<I", payload, 4)[0])}


# ═══════════════════════════════════════════════════════════
# HISTORICAL DATA FETCH (for warmup only)
# ═══════════════════════════════════════════════════════════
def fetch_historical_1min(sid, segment, days=5):
    to_d = now_ist().strftime("%Y-%m-%d")
    fr_d = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
    seg_inst_map = {"IDX_I": "INDEX", "NSE_FNO": "OPTIDX", "NSE_EQ": "EQUITY"}
    inst_type = seg_inst_map.get(segment, "INDEX")
    resp = api_post("/charts/intraday", {
        "securityId": str(sid), "exchangeSegment": segment,
        "instrument": inst_type, "interval": "1", "fromDate": fr_d, "toDate": to_d
    })
    if not resp or not resp.get("open"): return []
    candles = []
    ts_list = resp.get("timestamp") or resp.get("start_Time") or []
    for i in range(len(resp["open"])):
        t = _norm_epoch(int(ts_list[i])) if i < len(ts_list) else 0
        candles.append({"timestamp": t, "open": float(resp["open"][i]),
                        "high": float(resp["high"][i]), "low": float(resp["low"][i]),
                        "close": float(resp["close"][i])})
    log.info(f"Fetched {len(candles)} historical 1-min candles for {segment}:{sid}")
    return candles


# ═══════════════════════════════════════════════════════════
# LOT SIZE AUTO-FETCH
# ═══════════════════════════════════════════════════════════
DHAN_INSTRUMENT_API = "https://api.dhan.co/v2/instrument"
DHAN_COMPACT_CSV = "https://images.dhan.co/api-data/api-scrip-master.csv"

def fetch_lot_size(idx_name):
    """Auto-fetch lot size from Dhan instrument API for NIFTY/BANKNIFTY options."""
    if idx_name in LOT_SIZE_CACHE:
        return LOT_SIZE_CACHE[idx_name]

    symbol_root = "NIFTY" if idx_name == "NIFTY" else "BANKNIFTY"
    log.info(f"Fetching lot size for {idx_name} from Dhan instrument API...")

    rows = []
    # Try instrument API first
    try:
        r = requests.get(f"{DHAN_INSTRUMENT_API}/NSE_FNO", headers=HEADERS, timeout=60)
        if r.status_code == 200 and len(r.text) > 100:
            import io, csv
            rows = list(csv.DictReader(io.StringIO(r.text)))
            log.info(f"  Instrument API: {len(rows)} rows")
    except Exception as e:
        log.warning(f"  Instrument API failed: {e}")

    # Fallback to compact CSV
    if not rows:
        try:
            r = requests.get(DHAN_COMPACT_CSV, timeout=60); r.raise_for_status()
            import io, csv
            all_rows = list(csv.DictReader(io.StringIO(r.text)))
            rows = [x for x in all_rows if x.get("SEM_EXM_EXCH_ID") == "NSE" and x.get("SEM_SEGMENT") == "D"]
            log.info(f"  Compact CSV: {len(rows)} FNO rows")
        except Exception as e:
            log.warning(f"  Compact CSV failed: {e}")

    if not rows:
        log.warning(f"  Could not fetch lot size for {idx_name}, will try from option chain")
        return None

    # Find lot_size column
    lot_col = None
    sym_col = None
    inst_col = None
    sample = rows[0]
    for c in sample:
        cu = c.upper().replace("_", "")
        if "LOT" in cu and ("UNIT" in cu or "SIZE" in cu or "QTY" in cu):
            lot_col = c
        if "TRADINGSYMBOL" in cu or "SYMBOLNAME" in cu:
            sym_col = c
        if "INSTRUMENTNAME" in cu or "INSTRUMENT" in cu.replace("SEM", ""):
            inst_col = c

    if not lot_col:
        log.warning(f"  Could not find lot size column in instrument data")
        return None

    # Find any OPTIDX row for this index
    for row in rows:
        sym = row.get(sym_col, "").strip() if sym_col else ""
        inst = row.get(inst_col, "").strip() if inst_col else ""
        if inst == "OPTIDX" and sym.startswith(symbol_root):
            try:
                ls = int(float(row.get(lot_col, "0")))
                if ls > 0:
                    LOT_SIZE_CACHE[idx_name] = ls
                    log.info(f"  {idx_name} lot size = {ls} (auto-fetched)")
                    return ls
            except:
                continue

    log.warning(f"  Could not determine lot size for {idx_name}")
    return None


# ═══════════════════════════════════════════════════════════
# OPTION CHAIN HELPERS
# ═══════════════════════════════════════════════════════════
def resolve_option_strike(idx_name, option_type, strike_offset=0, expiry_choice="current"):
    idx_info = INDEX_MAP.get(idx_name)
    if not idx_info: return None
    # Get expiry
    resp = api_post("/optionchain/expirylist", {"UnderlyingScrip": int(idx_info["security_id"]), "UnderlyingSeg": "IDX_I"})
    if not resp or resp.get("status") != "success": return None
    today = now_ist().date()
    valid = sorted([(datetime.strptime(e, "%Y-%m-%d").date(), e) for e in resp.get("data", []) if datetime.strptime(e, "%Y-%m-%d").date() >= today])
    if not valid: return None
    expiry = valid[1][1] if expiry_choice == "next" and len(valid) > 1 else valid[0][1]
    # Get chain
    resp = api_post("/optionchain", {"UnderlyingScrip": int(idx_info["security_id"]), "UnderlyingSeg": "IDX_I", "Expiry": expiry})
    if not resp or resp.get("status") != "success":
        log.error(f"{idx_name}: Option chain fetch failed"); return None
    spot = float(resp["data"]["last_price"])
    oc = resp["data"]["oc"]
    atm = round(spot / idx_info["strike_gap"]) * idx_info["strike_gap"]
    target_strike = atm + (strike_offset * idx_info["strike_gap"])
    key = next((k for k in oc if abs(float(k) - target_strike) < 0.01), None)
    if not key or key not in oc: log.error(f"{idx_name}: Strike {target_strike} not in OC"); return None
    ok = "ce" if option_type == "CE" else "pe"
    if ok not in oc[key]: return None
    od = oc[key][ok]
    result = {"security_id": str(od["security_id"]), "strike": target_strike,
              "option_type": option_type, "last_price": float(od.get("last_price", 0)),
              "expiry": expiry, "spot": spot}
    # Auto-fetch lot size
    lot_size = fetch_lot_size(idx_name)
    if lot_size:
        result["lot_size"] = lot_size
    else:
        # Try from option chain response data
        try:
            ls = int(od.get("lot_size") or od.get("lotSize") or od.get("lot_qty") or 0)
            if ls > 0: result["lot_size"] = ls; LOT_SIZE_CACHE[idx_name] = ls
        except: pass
    log.info(f"Resolved {idx_name} {int(target_strike)}{option_type} | SecID={result['security_id']} | LTP={result['last_price']:.2f} | Exp={expiry} | Lot={result.get('lot_size', '?')}")
    return result


# ═══════════════════════════════════════════════════════════
# ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════
def place_order(security_id, exchange_segment, qty, buy_sell, product="INTRADAY"):
    payload = {"dhanClientId": DHAN_CLIENT_ID, "transactionType": buy_sell,
               "exchangeSegment": exchange_segment, "productType": product,
               "orderType": "MARKET", "validity": "DAY", "securityId": str(security_id),
               "quantity": int(qty), "price": 0, "triggerPrice": 0,
               "disclosedQuantity": 0, "afterMarketOrder": False}
    for attempt in range(3):
        log.info(f"ORDER | {buy_sell} {qty} | SecID={security_id} | Attempt {attempt+1}")
        resp = api_post("/orders", payload, retries=0)
        if resp and resp.get("orderId"):
            order_id = str(resp["orderId"])
            log.info(f"  Order placed | ID={order_id}")
            # Poll for fill
            for _ in range(10):
                time.sleep(0.5)
                trades = api_get(f"/trades/{order_id}", retries=0)
                if trades and isinstance(trades, list) and trades:
                    total_qty = sum(int(t.get("tradedQuantity", 0)) for t in trades)
                    total_val = sum(int(t.get("tradedQuantity", 0)) * float(t.get("tradedPrice", 0)) for t in trades)
                    if total_qty > 0:
                        fp = total_val / total_qty
                        log.info(f"  Fill | Price={fp:.2f} | Qty={total_qty}")
                        return order_id, fp
            return order_id, 0.0
        time.sleep(1)
    log.error(f"ORDER FAILED | {buy_sell} {qty} {security_id}")
    return None, 0.0


# ═══════════════════════════════════════════════════════════
# INDICATOR ENGINE (ChartIQ-matching)
# ═══════════════════════════════════════════════════════════
class IndicatorEngine:
    @staticmethod
    def calculate_bb(closes, period=20, multiplier=2.0):
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
        n = len(closes)
        rsi = [float('nan')] * n
        if n < period + 1: return rsi
        d = [closes[i] - closes[i - 1] for i in range(1, n)]
        p = [max(0, x) for x in d]
        n_abs = [abs(min(0, x)) for x in d]
        avgP = sum(p[:period]) / period
        avgL = sum(n_abs[:period]) / period
        rsi[period] = 100.0 if avgL == 0 else 100.0 - 100.0 / (1.0 + avgP / avgL)
        for i in range(period, len(d)):
            avgP = (avgP * (period - 1) + p[i]) / period
            avgL = (avgL * (period - 1) + n_abs[i]) / period
            rsi[i + 1] = 100.0 if avgL == 0 else 100.0 - 100.0 / (1.0 + avgP / avgL)
        return rsi


# ═══════════════════════════════════════════════════════════
# CANDLE BUILDER (from WebSocket ticks)
# ═══════════════════════════════════════════════════════════
class CandleBuilder:
    """Builds N-minute candles from tick-by-tick LTP data."""

    def __init__(self, interval_minutes):
        self.interval = interval_minutes
        self.candles = []  # Completed candles
        self.current = None  # Building candle: {open, high, low, close, bucket}

    def _get_bucket(self, dt):
        minutes_since_open = (dt.hour * 60 + dt.minute) - (9 * 60 + 15)
        if minutes_since_open < 0: minutes_since_open = 0
        return minutes_since_open // self.interval

    def process_tick(self, ltp, ts):
        """Process a tick. Returns completed candle dict if bucket boundary crossed, else None."""
        bucket = self._get_bucket(ts)
        epoch = int(ts.timestamp())

        if self.current is None:
            self.current = {"timestamp": epoch, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "bucket": bucket}
            return None

        if bucket != self.current["bucket"]:
            # Complete current candle
            completed = {"timestamp": self.current["timestamp"], "open": self.current["open"],
                         "high": self.current["high"], "low": self.current["low"], "close": self.current["close"]}
            self.candles.append(completed)
            # Start new candle
            self.current = {"timestamp": epoch, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "bucket": bucket}
            return completed
        else:
            # Update current candle
            self.current["high"] = max(self.current["high"], ltp)
            self.current["low"] = min(self.current["low"], ltp)
            self.current["close"] = ltp
            return None

    def get_building(self):
        if self.current:
            return {"timestamp": self.current["timestamp"], "open": self.current["open"],
                    "high": self.current["high"], "low": self.current["low"], "close": self.current["close"]}
        return None

    def build_from_history(self, candles_1min):
        """Warm up from historical 1-min candles."""
        for c in candles_1min:
            ts = datetime.fromtimestamp(c["timestamp"], tz=IST)
            bucket = self._get_bucket(ts)
            if self.current is None:
                self.current = {"timestamp": c["timestamp"], "open": c["open"], "high": c["high"],
                                "low": c["low"], "close": c["close"], "bucket": bucket}
            elif bucket != self.current["bucket"]:
                completed = {"timestamp": self.current["timestamp"], "open": self.current["open"],
                             "high": self.current["high"], "low": self.current["low"], "close": self.current["close"]}
                self.candles.append(completed)
                self.current = {"timestamp": c["timestamp"], "open": c["open"], "high": c["high"],
                                "low": c["low"], "close": c["close"], "bucket": bucket}
            else:
                self.current["high"] = max(self.current["high"], c["high"])
                self.current["low"] = min(self.current["low"], c["low"])
                self.current["close"] = c["close"]


# ═══════════════════════════════════════════════════════════
# STRATEGY DATA CLASSES
# ═══════════════════════════════════════════════════════════
@dataclass
class AlertCandle:
    timestamp: int; open: float; high: float; low: float; close: float
    bb_upper: float; bb_mid: float; bb_lower: float; rsi: float
    signal_type: str  # 'CE_SELL' or 'PE_SELL'

@dataclass
class ActiveTrade:
    trade_id: int; signal_type: str; option_type: str; security_id: str
    strike: float; entry_price: float; entry_time: datetime; qty: int; expiry: str
    spot_at_entry: float; alert_high: float; alert_low: float
    sl_spot_level: float  # SL on spot (alert high/low + buffer)
    sl_option_level: float  # SL on option (entry + max SL cap)
    target_type: str = ""; exit_price: float = 0.0; exit_time: Optional[datetime] = None
    pnl: float = 0.0; is_open: bool = True; current_ltp: float = 0.0
    order_id_entry: str = ""; order_id_exit: str = ""
    trailing_sl: float = 0.0  # Current trailing SL on option


# ═══════════════════════════════════════════════════════════
# STRATEGY ENGINE
# ═══════════════════════════════════════════════════════════
class StrategyEngine:
    def __init__(self, config):
        self.config = config
        self.indicator = IndicatorEngine()
        self.candle_builder = CandleBuilder(config["timeframe_minutes"])
        self.candle_closes = []
        self.candle_data = []

        # State
        self.alert_candle: Optional[AlertCandle] = None
        self.waiting_trigger = False
        self.active_trade: Optional[ActiveTrade] = None
        self.trade_history: List[ActiveTrade] = []
        self.trade_count = 0
        self.total_pnl = 0.0
        self.is_live = False

        # Latest indicator values for display
        self.latest_bb_upper = 0.0; self.latest_bb_mid = 0.0; self.latest_bb_lower = 0.0
        self.latest_rsi = 0.0; self.latest_close = 0.0; self.latest_candle_time = ""
        self.spot_ltp = 0.0; self.option_ltp = 0.0

        # Callbacks
        self.on_log = None
        self.on_ws_subscribe = None  # (security_id, exchange_segment) -> None
        self.on_ws_unsubscribe = None
        self.lock = threading.Lock()

    def _log(self, msg):
        log.info(msg)
        if self.on_log: self.on_log(msg)

    # ── SPOT TICK HANDLER ──
    def on_spot_tick(self, ltp, ts):
        """Called on every spot WebSocket tick."""
        self.spot_ltp = ltp

        with self.lock:
            if not self.is_live: return

            # Check force exit time
            fe_time = self.config.get("force_exit_time", "15:05")
            feh, fem = map(int, fe_time.split(":"))
            if ts.hour > feh or (ts.hour == feh and ts.minute >= fem):
                if self.active_trade and self.active_trade.is_open:
                    self._log(f"[{ts.strftime('%H:%M:%S')}] FORCE EXIT at {fe_time}")
                    self._execute_exit(self.active_trade, "TIME_EXIT", ts)
                return

            # If trade active: check SL Type 1 (spot-based)
            if self.active_trade and self.active_trade.is_open:
                trade = self.active_trade
                if trade.signal_type == "CE_SELL" and ltp >= trade.sl_spot_level:
                    self._log(f"[{ts.strftime('%H:%M:%S')}] ✗ SL HIT (SPOT) | LTP {ltp:.2f} >= SL {trade.sl_spot_level:.2f}")
                    self._execute_exit(trade, "SL_SPOT", ts)
                    return
                elif trade.signal_type == "PE_SELL" and ltp <= trade.sl_spot_level:
                    self._log(f"[{ts.strftime('%H:%M:%S')}] ✗ SL HIT (SPOT) | LTP {ltp:.2f} <= SL {trade.sl_spot_level:.2f}")
                    self._execute_exit(trade, "SL_SPOT", ts)
                    return

            # If waiting for trigger: check instant trigger
            if self.waiting_trigger and self.alert_candle:
                alert = self.alert_candle
                if alert.signal_type == "CE_SELL" and ltp < alert.low:
                    self._log(f"[{ts.strftime('%H:%M:%S')}] ▼ TRIGGER CE SELL | LTP {ltp:.2f} < Alert Low {alert.low:.2f}")
                    self._execute_entry(alert, ts)
                elif alert.signal_type == "PE_SELL" and ltp > alert.high:
                    self._log(f"[{ts.strftime('%H:%M:%S')}] ▲ TRIGGER PE SELL | LTP {ltp:.2f} > Alert High {alert.high:.2f}")
                    self._execute_entry(alert, ts)

        # Process tick for candle building (outside lock for performance)
        completed = self.candle_builder.process_tick(ltp, ts)
        if completed:
            self._process_completed_candle(completed)

    # ── OPTION TICK HANDLER ──
    def on_option_tick(self, ltp, ts):
        """Called on every option WebSocket tick."""
        self.option_ltp = ltp

        with self.lock:
            if not self.active_trade or not self.active_trade.is_open: return
            trade = self.active_trade
            trade.current_ltp = ltp

            # Check SL Type 2: Max cap on option premium
            if ltp >= trade.sl_option_level:
                self._log(f"[{ts.strftime('%H:%M:%S')}] ✗ SL HIT (OPTION) | LTP {ltp:.2f} >= SL {trade.sl_option_level:.2f}")
                self._execute_exit(trade, "SL_OPTION", ts)
                return

            # Check Universal Target
            universal_target = self.config.get("universal_target", 70)
            profit = trade.entry_price - ltp
            if profit >= universal_target:
                self._log(f"[{ts.strftime('%H:%M:%S')}] ✓ UNIVERSAL TARGET | Profit {profit:.2f} >= {universal_target}")
                self._execute_exit(trade, "UNIVERSAL_TARGET", ts)
                return

            # Check Trailing SL
            if self.config.get("trailing_enabled", False):
                self._check_trailing(trade, ltp, ts)

    # ── COMPLETED CANDLE HANDLER ──
    def _process_completed_candle(self, candle):
        """Process a completed N-minute candle for indicator calc and BB target."""
        with self.lock:
            self.candle_data.append(candle)
            self.candle_closes.append(candle["close"])

            bb_period = self.config.get("bb_period", 20)
            bb_mult = self.config.get("bb_multiplier", 2.0)
            rsi_period = self.config.get("rsi_period", 14)
            closes = self.candle_closes

            if len(closes) < max(bb_period, rsi_period + 1): return

            bb_upper, bb_mid, bb_lower = self.indicator.calculate_bb(closes, bb_period, bb_mult)
            rsi_vals = self.indicator.calculate_rsi(closes, rsi_period)

            curr_bb_upper = bb_upper[-1]; curr_bb_mid = bb_mid[-1]; curr_bb_lower = bb_lower[-1]
            curr_rsi = rsi_vals[-1]

            # Store latest values
            self.latest_bb_upper = curr_bb_upper; self.latest_bb_mid = curr_bb_mid
            self.latest_bb_lower = curr_bb_lower
            self.latest_rsi = curr_rsi if not math.isnan(curr_rsi) else 0.0
            self.latest_close = candle["close"]

            if not self.is_live: return

            c_open = candle["open"]; c_close = candle["close"]
            c_high = candle["high"]; c_low = candle["low"]
            ts = datetime.fromtimestamp(candle["timestamp"], tz=IST)
            self.latest_candle_time = ts.strftime('%H:%M')

            self._log(f"[{ts.strftime('%H:%M')}] O={c_open:.2f} H={c_high:.2f} L={c_low:.2f} C={c_close:.2f} | "
                      f"BB: {curr_bb_lower:.2f} / {curr_bb_mid:.2f} / {curr_bb_upper:.2f} | RSI: {curr_rsi:.2f}")

            # Time filter
            no_trade_time = self.config.get("no_trade_after", "14:45")
            h, m = map(int, no_trade_time.split(":"))
            past_cutoff = ts.hour > h or (ts.hour == h and ts.minute >= m)

            # If trade active: check BB target on candle close
            if self.active_trade and self.active_trade.is_open:
                trade = self.active_trade
                if trade.signal_type == "CE_SELL":
                    if not math.isnan(curr_bb_lower) and (c_close < curr_bb_lower or c_low < curr_bb_lower):
                        self._log(f"[{ts.strftime('%H:%M')}] ✓ BB TARGET (CE) | Close/Low below LBB {curr_bb_lower:.2f}")
                        self._execute_exit(trade, "BB_TARGET", ts)
                elif trade.signal_type == "PE_SELL":
                    if not math.isnan(curr_bb_upper) and (c_close > curr_bb_upper or c_high > curr_bb_upper):
                        self._log(f"[{ts.strftime('%H:%M')}] ✓ BB TARGET (PE) | Close/High above UBB {curr_bb_upper:.2f}")
                        self._execute_exit(trade, "BB_TARGET", ts)
                return

            # If waiting trigger and candle completed without trigger → cancel
            if self.waiting_trigger and self.alert_candle:
                alert = self.alert_candle
                if alert.signal_type == "CE_SELL":
                    self._log(f"[{ts.strftime('%H:%M')}] ✗ Trigger expired CE | Low {c_low:.2f} >= Alert Low {alert.low:.2f}")
                else:
                    self._log(f"[{ts.strftime('%H:%M')}] ✗ Trigger expired PE | High {c_high:.2f} <= Alert High {alert.high:.2f}")
                self.alert_candle = None; self.waiting_trigger = False

            if past_cutoff: return
            if self.active_trade and self.active_trade.is_open: return
            if math.isnan(curr_bb_upper) or math.isnan(curr_bb_lower): return

            # ALERT CHECK
            if c_open > curr_bb_upper and curr_bb_lower < c_close < curr_bb_upper:
                self._log(f"[{ts.strftime('%H:%M')}] ★ ALERT CE SELL | Open {c_open:.2f} > UBB {curr_bb_upper:.2f} | Close {c_close:.2f} inside bands")
                self.alert_candle = AlertCandle(timestamp=candle["timestamp"], open=c_open, high=c_high, low=c_low,
                    close=c_close, bb_upper=curr_bb_upper, bb_mid=curr_bb_mid, bb_lower=curr_bb_lower,
                    rsi=curr_rsi, signal_type="CE_SELL")
                self.waiting_trigger = True
            elif c_open < curr_bb_lower and curr_bb_lower < c_close < curr_bb_upper:
                self._log(f"[{ts.strftime('%H:%M')}] ★ ALERT PE SELL | Open {c_open:.2f} < LBB {curr_bb_lower:.2f} | Close {c_close:.2f} inside bands")
                self.alert_candle = AlertCandle(timestamp=candle["timestamp"], open=c_open, high=c_high, low=c_low,
                    close=c_close, bb_upper=curr_bb_upper, bb_mid=curr_bb_mid, bb_lower=curr_bb_lower,
                    rsi=curr_rsi, signal_type="PE_SELL")
                self.waiting_trigger = True

    # ── RSI CHECK ──
    def _check_spot_rsi(self, option_type):
        if not self.config.get("rsi_filter_enabled", True): return True
        ce_thresh = self.config.get("rsi_ce_threshold", 70)
        pe_thresh = self.config.get("rsi_pe_threshold", 30)
        rsi = self.latest_rsi
        if rsi == 0.0 or math.isnan(rsi): return True
        if option_type == "CE":
            self._log(f"  Spot RSI = {rsi:.2f} | CE Threshold >= {ce_thresh}")
            if rsi >= ce_thresh: return True
            self._log(f"  RSI BLOCKED (CE): {rsi:.2f} < {ce_thresh}"); return False
        else:
            self._log(f"  Spot RSI = {rsi:.2f} | PE Threshold <= {pe_thresh}")
            if rsi <= pe_thresh: return True
            self._log(f"  RSI BLOCKED (PE): {rsi:.2f} > {pe_thresh}"); return False

    # ── TRAILING SL (corrected mechanism) ──
    def _check_trailing(self, trade, option_ltp, ts):
        """
        Trailing SL: starts from initial SL level, tightens by Lock each Step.
        Initial SL = entry + max_sl_cap.  Each step: SL -= lock.
        """
        trail_step = self.config.get("trail_step", 10)
        trail_lock = self.config.get("trail_lock", 6)
        profit = trade.entry_price - option_ltp
        if profit <= 0: return

        steps = int(profit // trail_step)
        if steps < 1: return

        # Initial SL = entry + max_sl_cap (the sl_option_level)
        initial_sl = trade.sl_option_level
        new_trailing_sl = initial_sl - (steps * trail_lock)

        # Only tighten, never loosen
        if trade.trailing_sl == 0 or new_trailing_sl < trade.trailing_sl:
            old_sl = trade.trailing_sl if trade.trailing_sl > 0 else initial_sl
            trade.trailing_sl = new_trailing_sl
            self._log(f"[{ts.strftime('%H:%M:%S')}] ↕ TRAIL | Profit={profit:.2f} | Steps={steps} | SL: {old_sl:.2f} → {new_trailing_sl:.2f}")

        # Check if trailing SL hit
        if trade.trailing_sl > 0 and option_ltp >= trade.trailing_sl:
            self._log(f"[{ts.strftime('%H:%M:%S')}] ✓ TRAILING SL HIT | LTP={option_ltp:.2f} >= Trail SL={trade.trailing_sl:.2f}")
            self._execute_exit(trade, "TRAILING_SL", ts)

    # ── ENTRY ──
    def _execute_entry(self, alert: AlertCandle, ts: datetime):
        config = self.config
        idx_name = config["index"]
        option_type = "CE" if alert.signal_type == "CE_SELL" else "PE"
        mode = config.get("trade_mode", "paper")
        buffer_pts = config.get("sl_buffer", 5)
        max_sl = config.get("max_sl", 25)

        # RSI filter first (no API call)
        if not self._check_spot_rsi(option_type):
            self._log(f"  ENTRY BLOCKED by spot RSI filter"); self.alert_candle = None; self.waiting_trigger = False; return

        # Resolve option strike
        self._log(f"  Resolving {idx_name} {option_type} (offset={config.get('strike_offset', 0)})...")
        opt = resolve_option_strike(idx_name, option_type, config.get("strike_offset", 0), config.get("expiry", "current"))
        if not opt:
            self._log(f"  ENTRY FAILED: Could not resolve strike"); self.alert_candle = None; self.waiting_trigger = False; return

        lot_size = opt.get("lot_size")
        if not lot_size:
            self._log(f"  WARNING: Could not determine lot size, using fallback")
            lot_size = 75 if idx_name == "NIFTY" else 30
        qty = lot_size * config.get("lots", 1)
        entry_price = opt["last_price"]
        spot = opt["spot"]

        # SL Type 1: Alert-based on spot
        sl_spot = (alert.high + buffer_pts) if alert.signal_type == "CE_SELL" else (alert.low - buffer_pts)
        # SL Type 2: Max cap on option
        sl_option = entry_price + max_sl

        # Place order
        order_id = ""
        if mode == "live":
            oid, fp = place_order(opt["security_id"], "NSE_FNO", qty, "SELL")
            if not oid:
                self._log("  ENTRY ORDER FAILED"); self.alert_candle = None; self.waiting_trigger = False; return
            order_id = oid
            if fp > 0: entry_price = fp
            sl_option = entry_price + max_sl  # Recalc with fill price

        self.trade_count += 1
        trade = ActiveTrade(
            trade_id=self.trade_count, signal_type=alert.signal_type, option_type=option_type,
            security_id=opt["security_id"], strike=opt["strike"], entry_price=entry_price,
            entry_time=ts, qty=qty, expiry=opt["expiry"], spot_at_entry=spot,
            alert_high=alert.high, alert_low=alert.low,
            sl_spot_level=sl_spot, sl_option_level=sl_option,
            current_ltp=entry_price, order_id_entry=order_id)
        self.active_trade = trade
        self.alert_candle = None; self.waiting_trigger = False

        self._log(f"  ✓ ENTRY #{self.trade_count} | SELL {option_type} {int(opt['strike'])} "
                  f"| Exp={opt['expiry']} | Price={entry_price:.2f} | Qty={qty} (lot={lot_size}) "
                  f"| SL(spot)={sl_spot:.2f} | SL(opt)={sl_option:.2f} | {mode.upper()}")

        # Log to trade CSV
        alert_time = datetime.fromtimestamp(alert.timestamp, tz=IST).strftime('%H:%M:%S') if alert.timestamp else ""
        trade_logger.log_entry(self.trade_count, idx_name, option_type, opt["strike"], opt["expiry"],
                               alert_time, ts.strftime('%H:%M:%S'), entry_price, qty, lot_size)

        # Subscribe to option WebSocket feed
        if self.on_ws_subscribe:
            self.on_ws_subscribe(opt["security_id"], "NSE_FNO")

    # ── EXIT ──
    def _execute_exit(self, trade: ActiveTrade, reason: str, ts: datetime):
        mode = self.config.get("trade_mode", "paper")
        exit_price = trade.current_ltp if trade.current_ltp > 0 else trade.entry_price

        if mode == "live":
            oid, fp = place_order(trade.security_id, "NSE_FNO", trade.qty, "BUY")
            if oid:
                trade.order_id_exit = oid
                if fp > 0: exit_price = fp
            else:
                self._log("  EXIT ORDER FAILED — manual exit needed!")

        trade.exit_price = exit_price; trade.exit_time = ts
        trade.pnl = (trade.entry_price - exit_price) * trade.qty
        trade.is_open = False; trade.target_type = reason
        self.total_pnl += trade.pnl
        self.trade_history.append(trade)

        # Unsubscribe from option feed
        if self.on_ws_unsubscribe:
            self.on_ws_unsubscribe(trade.security_id, "NSE_FNO")
        self.active_trade = None

        self._log(f"  ✓ EXIT #{trade.trade_id} | {reason} | {trade.option_type} {int(trade.strike)} "
                  f"| {trade.entry_price:.2f} → {exit_price:.2f} | PnL={trade.pnl:+.2f} | Total={self.total_pnl:+.2f}")

        # Log to trade CSV
        trade_logger.log_exit(trade.trade_id, ts.strftime('%H:%M:%S'), exit_price, reason, trade.pnl)

    def force_exit(self, reason="FORCE_EXIT"):
        with self.lock:
            if self.active_trade and self.active_trade.is_open:
                self._execute_exit(self.active_trade, reason, now_ist())

    def warmup(self, idx_name):
        """Load historical data for indicator warmup."""
        idx_info = INDEX_MAP[idx_name]
        candles_1min = fetch_historical_1min(idx_info["security_id"], idx_info["segment"], days=5)
        if candles_1min:
            log.info(f"Building from {len(candles_1min)} historical 1-min candles...")
            self.candle_builder.build_from_history(candles_1min)
            # Process completed candles for indicator warmup
            for c in self.candle_builder.candles:
                self.candle_data.append(c)
                self.candle_closes.append(c["close"])
            # Calculate indicators on full history
            if len(self.candle_closes) >= 20:
                bb_u, bb_m, bb_l = self.indicator.calculate_bb(self.candle_closes, self.config.get("bb_period", 20), self.config.get("bb_multiplier", 2.0))
                rsi = self.indicator.calculate_rsi(self.candle_closes, 14)
                self.latest_bb_upper = bb_u[-1]; self.latest_bb_mid = bb_m[-1]; self.latest_bb_lower = bb_l[-1]
                self.latest_rsi = rsi[-1] if not math.isnan(rsi[-1]) else 0.0
                self.latest_close = self.candle_closes[-1]
            log.info(f"Warmup done. {len(self.candle_data)} candles, BB/RSI initialized.")
        self.is_live = True
        log.info("Live mode enabled — scanning for signals")


# ═══════════════════════════════════════════════════════════
# LIVE ENGINE (WebSocket-based)
# ═══════════════════════════════════════════════════════════
class LiveEngine:
    def __init__(self, strategies: Dict[str, StrategyEngine], config: dict):
        self.strategies = strategies  # idx_name -> StrategyEngine
        self.config = config
        self.stop_event = threading.Event()
        self.ws_connected = threading.Event()
        self.ws = None
        self.ws_lock = threading.Lock()
        self.secid_map = {}  # security_id -> ("spot", idx_name) or ("option", idx_name)

        # Wire up WebSocket callbacks for each strategy
        for idx_name, strat in self.strategies.items():
            strat.on_ws_subscribe = lambda sid, exch, idx=idx_name: self._ws_subscribe(sid, exch, idx)
            strat.on_ws_unsubscribe = lambda sid, exch, idx=idx_name: self._ws_unsubscribe(sid, exch, idx)

    def _ws_subscribe(self, sid, exch, idx_name):
        self.secid_map[str(sid)] = ("option", idx_name)
        if self.ws and self.ws_connected.is_set():
            try:
                with self.ws_lock:
                    self.ws.send(json.dumps({"RequestCode": REQ_SUB_TICKER, "InstrumentCount": 1,
                                            "InstrumentList": [{"ExchangeSegment": exch, "SecurityId": str(sid)}]}))
                log.info(f"WS subscribed option: {exch}:{sid} ({idx_name})")
            except Exception as e: log.error(f"WS sub failed: {e}")

    def _ws_unsubscribe(self, sid, exch, idx_name):
        self.secid_map.pop(str(sid), None)
        if self.ws and self.ws_connected.is_set():
            try:
                with self.ws_lock:
                    self.ws.send(json.dumps({"RequestCode": REQ_UNSUB_TICKER, "InstrumentCount": 1,
                                            "InstrumentList": [{"ExchangeSegment": exch, "SecurityId": str(sid)}]}))
            except: pass

    def on_ws_open(self, ws):
        self.ws_connected.set()
        insts = []
        for idx_name in self.strategies:
            idx_info = INDEX_MAP[idx_name]
            sid = idx_info["security_id"]; seg = idx_info["segment"]
            self.secid_map[sid] = ("spot", idx_name)
            insts.append({"ExchangeSegment": seg, "SecurityId": sid})
        ws.send(json.dumps({"RequestCode": REQ_SUB_TICKER, "InstrumentCount": len(insts), "InstrumentList": insts}))
        names = ", ".join(self.strategies.keys())
        log.info(f"WS connected — subscribed to {names} ({len(insts)} instruments)")

    def on_ws_message(self, ws, message):
        if isinstance(message, str): return
        hdr = parse_header_8(bytes(message))
        if not hdr: return
        code = int(hdr["resp_code"]); sid = str(hdr["security_id"])
        if code == RESP_TICKER:
            t = parse_ticker(hdr["payload"])
            if not t: return
            ltp = float(t["ltp"]); ltt = _norm_epoch(int(t["ltt_epoch"]))
            ts = datetime.fromtimestamp(ltt, tz=IST)
            info = self.secid_map.get(sid)
            if not info: return
            role, idx_name = info
            strat = self.strategies.get(idx_name)
            if not strat: return
            if role == "spot":
                strat.on_spot_tick(ltp, ts)
            elif role == "option":
                strat.on_option_tick(ltp, ts)
        elif code == RESP_DISCONNECT:
            log.warning(f"WS disconnect signal: {hdr}")

    def on_ws_error(self, ws, error):
        log.warning(f"WS error: {error}")

    def on_ws_close(self, ws, sc, msg):
        self.ws_connected.clear()
        log.warning(f"WS closed: {sc} {msg}")

    def run(self):
        """Main entry: warmup all strategies then WebSocket loop."""
        try:
            for idx_name, strat in self.strategies.items():
                strat.warmup(idx_name)

            # WebSocket loop with auto-reconnect
            websocket.enableTrace(False)
            while not self.stop_event.is_set():
                try:
                    self.ws = websocket.WebSocketApp(WS_URL,
                        on_open=self.on_ws_open, on_message=self.on_ws_message,
                        on_error=self.on_ws_error, on_close=self.on_ws_close)
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as e:
                    log.error(f"WS exception: {e}")
                finally:
                    self.ws_connected.clear()
                    if not self.stop_event.is_set():
                        log.info("WS reconnecting in 2s...")
                        time.sleep(2)
        except Exception as e:
            log.error(f"FATAL: LiveEngine crashed: {e}", exc_info=True)

    def stop(self):
        self.stop_event.set()
        if self.ws: self.ws.close()


# ═══════════════════════════════════════════════════════════
# MODERN UI — CustomTkinter
# ═══════════════════════════════════════════════════════════
class BBRSIApp(ctk.CTk):
    CLR_BG="#f7f8fc"; CLR_PANEL="#ffffff"; CLR_CARD="#f0f4ff"; CLR_HEADER="#1a56db"
    CLR_ACCENT="#2563eb"; CLR_ACCENT_L="#3b82f6"; CLR_GREEN="#16a34a"; CLR_RED="#dc2626"
    CLR_ORANGE="#ea580c"; CLR_TEXT="#1e293b"; CLR_MUTED="#64748b"; CLR_BORDER="#e2e8f0"
    CLR_INPUT_BG="#f1f5f9"; CLR_LOG_BG="#fafbfe"; FONT="Consolas"; FONT_UI="Segoe UI"

    def __init__(self):
        super().__init__()
        self.title("BB-RSI Option Seller  |  Balfund Trading Pvt. Ltd.")
        self.geometry("1220x880"); self.minsize(1000, 700)
        self.configure(fg_color=self.CLR_BG)
        ctk.set_appearance_mode("light"); ctk.set_default_color_theme("blue")
        self.engine: Optional[LiveEngine] = None
        self.engine_thread: Optional[threading.Thread] = None
        self.strategies: Dict[str, StrategyEngine] = {}
        self.connected = False; self.log_buffer = deque(maxlen=300)
        self._build_ui(); self._load_env_values()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _lbl(self, p, t, bold=False):
        f = (self.FONT_UI, 11, "bold") if bold else (self.FONT_UI, 11)
        return ctk.CTkLabel(p, text=t, font=f, text_color=self.CLR_TEXT)
    def _ent(self, p, v, w=55):
        return ctk.CTkEntry(p, width=w, textvariable=v, fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, border_width=1, text_color=self.CLR_TEXT, font=(self.FONT, 11))
    def _dd(self, p, v, vals, w=115):
        return ctk.CTkOptionMenu(p, variable=v, values=vals, width=w, fg_color=self.CLR_INPUT_BG, button_color=self.CLR_ACCENT, button_hover_color=self.CLR_ACCENT_L, dropdown_fg_color=self.CLR_PANEL, text_color=self.CLR_TEXT, font=(self.FONT_UI, 11), dropdown_font=(self.FONT_UI, 11))

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(2, weight=1)
        hdr = ctk.CTkFrame(self, fg_color=self.CLR_HEADER, corner_radius=0, height=56)
        hdr.grid(row=0, column=0, sticky="ew"); hdr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(hdr, text="  BB-RSI Option Seller", font=(self.FONT_UI, 18, "bold"), text_color="#ffffff").grid(row=0, column=0, padx=16, pady=14, sticky="w")
        ctk.CTkLabel(hdr, text="Balfund Trading Pvt. Ltd.  |  v2.2", font=(self.FONT_UI, 11), text_color="#bfdbfe").grid(row=0, column=1, padx=5, pady=14, sticky="w")
        self.status_label = ctk.CTkLabel(hdr, text="\u25cf DISCONNECTED", font=(self.FONT, 11, "bold"), text_color="#fca5a5")
        self.status_label.grid(row=0, column=2, padx=16, pady=14)

        sf = ctk.CTkFrame(self, fg_color=self.CLR_PANEL, border_color=self.CLR_BORDER, border_width=1, corner_radius=8)
        sf.grid(row=1, column=0, sticky="ew", padx=10, pady=(6, 0))

        r1 = ctk.CTkFrame(sf, fg_color="transparent"); r1.pack(fill="x", padx=14, pady=(10, 5))
        self._lbl(r1, "Client ID", True).pack(side="left", padx=(0, 4))
        self.client_id_entry = ctk.CTkEntry(r1, width=120, placeholder_text="Client ID", fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, border_width=1, text_color=self.CLR_TEXT, font=(self.FONT, 11)); self.client_id_entry.pack(side="left", padx=3)
        self._lbl(r1, "PIN", True).pack(side="left", padx=(14, 4))
        self.pin_entry = ctk.CTkEntry(r1, width=80, show="*", placeholder_text="PIN", fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, border_width=1, text_color=self.CLR_TEXT, font=(self.FONT, 11)); self.pin_entry.pack(side="left", padx=3)
        self._lbl(r1, "TOTP Secret", True).pack(side="left", padx=(14, 4))
        self.totp_entry = ctk.CTkEntry(r1, width=200, show="*", placeholder_text="Base32 secret", fg_color=self.CLR_INPUT_BG, border_color=self.CLR_BORDER, border_width=1, text_color=self.CLR_TEXT, font=(self.FONT, 11)); self.totp_entry.pack(side="left", padx=3)
        self.connect_btn = ctk.CTkButton(r1, text="Connect", width=110, height=32, fg_color=self.CLR_ACCENT, hover_color=self.CLR_ACCENT_L, text_color="#ffffff", font=(self.FONT_UI, 11, "bold"), command=self._connect); self.connect_btn.pack(side="left", padx=(20, 0))

        r2 = ctk.CTkFrame(sf, fg_color="transparent"); r2.pack(fill="x", padx=14, pady=4)
        self._lbl(r2, "Index", True).pack(side="left", padx=(0, 4))
        self.nifty_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(r2, text="NIFTY", variable=self.nifty_var, font=(self.FONT_UI, 11, "bold"), text_color=self.CLR_TEXT, fg_color=self.CLR_ACCENT, border_color=self.CLR_BORDER).pack(side="left", padx=(0, 8))
        self.bnf_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r2, text="BANKNIFTY", variable=self.bnf_var, font=(self.FONT_UI, 11, "bold"), text_color=self.CLR_TEXT, fg_color=self.CLR_ACCENT, border_color=self.CLR_BORDER).pack(side="left", padx=(0, 10))
        self._lbl(r2, "Timeframe", True).pack(side="left", padx=(10, 4)); self.tf_var = ctk.StringVar(value="3 Minutes"); self._dd(r2, self.tf_var, list(TIMEFRAME_MAP.keys()), 125).pack(side="left", padx=3)
        self._lbl(r2, "Strike Offset", True).pack(side="left", padx=(10, 4)); self.offset_var = ctk.StringVar(value="0"); self._ent(r2, self.offset_var, 45).pack(side="left", padx=3)
        self._lbl(r2, "Expiry", True).pack(side="left", padx=(10, 4)); self.expiry_var = ctk.StringVar(value="current"); self._dd(r2, self.expiry_var, ["current", "next"], 95).pack(side="left", padx=3)
        self._lbl(r2, "Lots", True).pack(side="left", padx=(10, 4)); self.lots_var = ctk.StringVar(value="1"); self._ent(r2, self.lots_var, 40).pack(side="left", padx=3)
        self._lbl(r2, "Mode", True).pack(side="left", padx=(10, 4)); self.mode_var = ctk.StringVar(value="paper"); self._dd(r2, self.mode_var, ["paper", "live"], 85).pack(side="left", padx=3)

        r3 = ctk.CTkFrame(sf, fg_color="transparent"); r3.pack(fill="x", padx=14, pady=4)
        for lbl, vn, dv, w in [("BB Period","bb_period_var","20",42),("BB Mult","bb_mult_var","2.0",42),("SL Buffer","sl_buffer_var","5",42),("Max SL","max_sl_var","25",42),("Target","target_var","70",42),("No Trade","no_trade_var","14:45",58),("Force Exit","force_exit_var","15:05",58)]:
            self._lbl(r3, lbl, True).pack(side="left", padx=(10 if lbl != "BB Period" else 0, 3))
            v = ctk.StringVar(value=dv); setattr(self, vn, v); self._ent(r3, v, w).pack(side="left", padx=2)

        r4 = ctk.CTkFrame(sf, fg_color="transparent"); r4.pack(fill="x", padx=14, pady=(4, 10))
        self.rsi_enabled_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(r4, text="RSI Filter", variable=self.rsi_enabled_var, font=(self.FONT_UI, 11, "bold"), text_color=self.CLR_TEXT, fg_color=self.CLR_ACCENT, border_color=self.CLR_BORDER).pack(side="left", padx=(0, 6))
        self._lbl(r4, "CE \u2265", True).pack(side="left", padx=(4, 2)); self.rsi_ce_var = ctk.StringVar(value="70"); self._ent(r4, self.rsi_ce_var, 40).pack(side="left", padx=2)
        self._lbl(r4, "PE \u2264", True).pack(side="left", padx=(8, 2)); self.rsi_pe_var = ctk.StringVar(value="30"); self._ent(r4, self.rsi_pe_var, 40).pack(side="left", padx=2)
        ctk.CTkFrame(r4, fg_color=self.CLR_BORDER, width=1, height=26).pack(side="left", padx=14)
        self.trail_enabled_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r4, text="Trailing", variable=self.trail_enabled_var, font=(self.FONT_UI, 11, "bold"), text_color=self.CLR_TEXT, fg_color=self.CLR_ACCENT, border_color=self.CLR_BORDER).pack(side="left", padx=(0, 6))
        self._lbl(r4, "Step", True).pack(side="left", padx=(4, 2)); self.trail_step_var = ctk.StringVar(value="10"); self._ent(r4, self.trail_step_var, 40).pack(side="left", padx=2)
        self._lbl(r4, "Lock", True).pack(side="left", padx=(8, 2)); self.trail_lock_var = ctk.StringVar(value="6"); self._ent(r4, self.trail_lock_var, 40).pack(side="left", padx=2)
        ctk.CTkFrame(r4, fg_color=self.CLR_BORDER, width=1, height=26).pack(side="left", padx=14)
        self.start_btn = ctk.CTkButton(r4, text="\u25b6  START", width=100, height=34, fg_color=self.CLR_GREEN, hover_color="#15803d", text_color="#ffffff", font=(self.FONT_UI, 12, "bold"), command=self._start_strategy, state="disabled"); self.start_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ctk.CTkButton(r4, text="\u25a0  STOP", width=100, height=34, fg_color=self.CLR_RED, hover_color="#b91c1c", text_color="#ffffff", font=(self.FONT_UI, 12, "bold"), command=self._stop_strategy, state="disabled"); self.stop_btn.pack(side="left", padx=6)
        self.exit_btn = ctk.CTkButton(r4, text="\u26a1 EXIT NOW", width=110, height=34, fg_color=self.CLR_ORANGE, hover_color="#c2410c", text_color="#ffffff", font=(self.FONT_UI, 12, "bold"), command=self._force_exit_trade, state="disabled"); self.exit_btn.pack(side="left", padx=6)

        btm = ctk.CTkFrame(self, fg_color="transparent"); btm.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)
        btm.grid_columnconfigure(0, weight=2); btm.grid_columnconfigure(1, weight=3); btm.grid_rowconfigure(0, weight=1)
        tf = ctk.CTkFrame(btm, fg_color=self.CLR_PANEL, border_color=self.CLR_BORDER, border_width=1, corner_radius=8); tf.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ctk.CTkLabel(tf, text="TRADE STATUS", font=(self.FONT_UI, 13, "bold"), text_color=self.CLR_ACCENT).pack(pady=(10, 5))
        self.trade_info_text = ctk.CTkTextbox(tf, font=(self.FONT, 11), fg_color=self.CLR_CARD, text_color=self.CLR_TEXT, border_color=self.CLR_BORDER, border_width=1, state="disabled", wrap="word"); self.trade_info_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        lf = ctk.CTkFrame(btm, fg_color=self.CLR_PANEL, border_color=self.CLR_BORDER, border_width=1, corner_radius=8); lf.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        ctk.CTkLabel(lf, text="STRATEGY LOG", font=(self.FONT_UI, 13, "bold"), text_color=self.CLR_ACCENT).pack(pady=(10, 5))
        self.log_text = ctk.CTkTextbox(lf, font=(self.FONT, 10), fg_color=self.CLR_LOG_BG, text_color=self.CLR_MUTED, border_color=self.CLR_BORDER, border_width=1, state="disabled", wrap="word"); self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        ft = ctk.CTkFrame(self, fg_color=self.CLR_HEADER, corner_radius=0, height=28); ft.grid(row=3, column=0, sticky="ew")
        ctk.CTkLabel(ft, text="\u00a9 Balfund Trading Pvt Ltd  |  www.balfund.com  |  info@balfund.com", font=(self.FONT_UI, 10), text_color="#93c5fd").pack(pady=4)
        self._refresh_dashboard()

    def _load_env_values(self):
        for key, entry in [("DHAN_CLIENT_ID", self.client_id_entry), ("DHAN_PIN", self.pin_entry), ("DHAN_TOTP_SECRET", self.totp_entry)]:
            v = os.getenv(key, "")
            if v: entry.insert(0, v)

    def _connect(self):
        cid = self.client_id_entry.get().strip(); pin = self.pin_entry.get().strip(); totp = self.totp_entry.get().strip()
        if not all([cid, pin, totp]): self._append_log("ERROR: Fill in all credentials"); return
        set_key(str(ENV_FILE), "DHAN_CLIENT_ID", cid); set_key(str(ENV_FILE), "DHAN_PIN", pin); set_key(str(ENV_FILE), "DHAN_TOTP_SECRET", totp)
        os.environ["DHAN_CLIENT_ID"] = cid; os.environ["DHAN_PIN"] = pin; os.environ["DHAN_TOTP_SECRET"] = totp
        self.connect_btn.configure(state="disabled", text="Connecting..."); self._append_log("Connecting to Dhan...")
        def _do(): ok, msg = init_credentials(); self.after(0, lambda: self._on_connect(ok, msg))
        threading.Thread(target=_do, daemon=True).start()

    def _on_connect(self, ok, msg):
        if ok:
            self.connected = True; self.status_label.configure(text="\u25cf CONNECTED", text_color="#22c55e")
            self.connect_btn.configure(text="Connected \u2713", fg_color=self.CLR_GREEN); self.start_btn.configure(state="normal")
            self._append_log("Connected to Dhan")
        else:
            self.status_label.configure(text="\u25cf FAILED", text_color=self.CLR_RED)
            self.connect_btn.configure(state="normal", text="Connect"); self._append_log(f"Failed: {msg}")

    def _get_config(self):
        indices = []
        if self.nifty_var.get(): indices.append("NIFTY")
        if self.bnf_var.get(): indices.append("BANKNIFTY")
        return {"indices": indices, "timeframe_minutes": TIMEFRAME_MAP.get(self.tf_var.get(), 3),
                "strike_offset": int(self.offset_var.get() or 0), "expiry": self.expiry_var.get(),
                "lots": int(self.lots_var.get() or 1), "trade_mode": self.mode_var.get(),
                "bb_period": int(self.bb_period_var.get() or 20), "bb_multiplier": float(self.bb_mult_var.get() or 2.0),
                "sl_buffer": int(self.sl_buffer_var.get() or 5), "max_sl": int(self.max_sl_var.get() or 25),
                "universal_target": int(self.target_var.get() or 70),
                "no_trade_after": self.no_trade_var.get() or "14:45", "force_exit_time": self.force_exit_var.get() or "15:05",
                "rsi_filter_enabled": self.rsi_enabled_var.get(),
                "rsi_ce_threshold": int(self.rsi_ce_var.get() or 70), "rsi_pe_threshold": int(self.rsi_pe_var.get() or 30),
                "trailing_enabled": self.trail_enabled_var.get(),
                "trail_step": int(self.trail_step_var.get() or 10), "trail_lock": int(self.trail_lock_var.get() or 6),
                "rsi_period": 14}

    def _start_strategy(self):
        if not self.connected: self._append_log("ERROR: Connect first"); return
        config = self._get_config()
        if not config["indices"]: self._append_log("ERROR: Select at least one index"); return
        names = " + ".join(config["indices"])
        self._append_log(f"Starting: {names} | TF={config['timeframe_minutes']}min | Mode={config['trade_mode']}")
        self.strategies = {}
        for idx in config["indices"]:
            idx_config = dict(config); idx_config["index"] = idx
            strat = StrategyEngine(idx_config)
            strat.on_log = lambda msg, i=idx: self.after(0, lambda m=msg: self._append_log(f"[{i}] {m}"))
            self.strategies[idx] = strat
        self.engine = LiveEngine(self.strategies, config)
        self.engine_thread = threading.Thread(target=self.engine.run, daemon=True); self.engine_thread.start()
        self.start_btn.configure(state="disabled"); self.stop_btn.configure(state="normal"); self.exit_btn.configure(state="normal")

    def _stop_strategy(self):
        if self.engine: self.engine.stop()
        for s in self.strategies.values():
            if s.active_trade and s.active_trade.is_open: s.force_exit("MANUAL_STOP")
        self.start_btn.configure(state="normal"); self.stop_btn.configure(state="disabled"); self.exit_btn.configure(state="disabled")
        self._append_log("Strategy stopped.")

    def _force_exit_trade(self):
        for s in self.strategies.values(): s.force_exit("MANUAL_EXIT")

    def _append_log(self, msg):
        ts = now_ist().strftime("%H:%M:%S"); line = f"[{ts}] {msg}"
        self.log_text.configure(state="normal"); self.log_text.insert("end", line + "\n"); self.log_text.see("end"); self.log_text.configure(state="disabled")

    def _refresh_dashboard(self):
        if self.strategies:
            lines = []
            total_pnl = sum(s.total_pnl for s in self.strategies.values())
            total_trades = sum(s.trade_count for s in self.strategies.values())
            eq = "=" * 42
            lines.append(eq)
            lines.append(f"  Indices: {', '.join(self.strategies.keys())}")
            lines.append(f"  Trades: {total_trades}  |  Total PnL: {total_pnl:+.2f}")
            lines.append(eq)
            for idx_name, s in self.strategies.items():
                lines.append(f"\n  \u2501\u2501 {idx_name} \u2501\u2501")
                lines.append(f"  Candles: {len(s.candle_data)}  |  LTP: {s.spot_ltp:.2f}")
                if s.latest_candle_time:
                    lines.append(f"  BB: {s.latest_bb_lower:.2f} / {s.latest_bb_mid:.2f} / {s.latest_bb_upper:.2f}")
                    lines.append(f"  RSI: {s.latest_rsi:.2f}  ({s.latest_candle_time})")
                if s.waiting_trigger and s.alert_candle:
                    a = s.alert_candle
                    lines.append(f"  \u2605 ALERT ({a.signal_type}) | H={a.high:.2f} L={a.low:.2f}")
                if s.active_trade and s.active_trade.is_open:
                    t = s.active_trade
                    pnl = (t.entry_price - t.current_ltp) * t.qty if t.current_ltp > 0 else 0
                    lines.append(f"  \u26a1 #{t.trade_id} SELL {t.option_type} {int(t.strike)} | E={t.entry_price:.2f} LTP={t.current_ltp:.2f}")
                    lines.append(f"    PnL={pnl:+.2f} | SL(s)={t.sl_spot_level:.2f} SL(o)={t.sl_option_level:.2f}")
                    if t.trailing_sl > 0:
                        lines.append(f"    Trail SL: {t.trailing_sl:.2f}")
                elif not s.waiting_trigger:
                    lines.append("  FLAT \u2014 Scanning...")
                for t in s.trade_history[-3:]:
                    p = "+" if t.pnl >= 0 else ""
                    lines.append(f"  #{t.trade_id} {t.option_type}{int(t.strike)} {t.entry_price:.2f}\u2192{t.exit_price:.2f} {p}{t.pnl:.2f} {t.target_type}")
            self.trade_info_text.configure(state="normal"); self.trade_info_text.delete("1.0", "end")
            self.trade_info_text.insert("1.0", "\n".join(lines)); self.trade_info_text.configure(state="disabled")
        self.after(2000, self._refresh_dashboard)

    def _on_close(self):
        if self.engine: self.engine.stop()
        for s in self.strategies.values():
            if s.active_trade and s.active_trade.is_open: s.force_exit("APP_CLOSE")
        self.destroy()

def main():
    app = BBRSIApp(); app.mainloop()

if __name__ == "__main__":
    main()
