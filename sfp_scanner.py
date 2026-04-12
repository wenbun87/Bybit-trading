#!/usr/bin/env python3
"""
Bybit SFP Scanner + Auto-Trader + Position Manager (all-in-one)

Scans high-volume Bybit perpetuals for Swing Failure Patterns and
optionally auto-trades them with adaptive trailing stop exits.

  Bearish SFP → Short entry (wick sweeps swing high, closes below)
  Bullish SFP → Long entry  (wick sweeps swing low, closes above)

Exit management (same trailing stop tiers as the momentum auto-trader):
  -5% initial SL → 10%+ → 5% trail | 30%+ → 3% | 100%+ → 2% | 300%+ → 1.5%

Usage:
    python3 sfp_scanner.py                     # scan only (no trades)
    python3 sfp_scanner.py --watch 30          # rescan every 30 min
    python3 sfp_scanner.py --trade             # dry-run auto-trading
    python3 sfp_scanner.py --trade --live      # REAL trades + position mgmt
    python3 sfp_scanner.py --trade --live --amount 500 --leverage 5
    python3 sfp_scanner.py --timeframe 4h      # only scan 4h timeframe
    python3 sfp_scanner.py --min-grade B       # only trade B grade or better

REQUIRES (for --trade mode):
  export BYBIT_API_KEY="your_key"
  export BYBIT_API_SECRET="your_secret"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
USER_AGENT = "bybit-skill/1.2.3"
MIN_CALL_INTERVAL = 0.12  # 120ms between GET requests

# SFP detection parameters
DEFAULT_PIVOT_LOOKBACK = 5       # bars left + right to confirm a swing point
DEFAULT_MIN_SWEEP_PCT = 0.05     # wick must exceed swing level by at least 0.05%
DEFAULT_MAX_CANDLES_AGO = 3      # only report SFPs from last N candles
DEFAULT_MIN_VOLUME_M = 50        # minimum 24h turnover in millions USD

# Timeframe mapping: label → (bybit interval, candles to fetch)
TIMEFRAMES = {
    "15m": ("15", 120),    # 120 x 15m = 30 hours
    "1h":  ("60", 100),    # 100 x 1h  = ~4 days
    "4h":  ("240", 80),    # 80  x 4h  = ~13 days
    "1d":  ("D", 60),      # 60 days
}

DEFAULT_TIMEFRAMES = ["1h", "4h"]

# Trading config
RECV_WINDOW = "5000"
DEFAULT_AMOUNT_USDT = 500
DEFAULT_LEVERAGE = 5
MAX_TRADES_PER_CYCLE = 2
MAX_TRADES_PER_DAY = 6
DEFAULT_INITIAL_SL_PCT = 8.0
DEFAULT_MIN_TRADE_GRADE = "B"  # only trade B or better

TRAILING_TIERS = [
    (0,    0),
    (10,   5.0),
    (30,   3.0),
    (100,  2.0),
    (300,  1.5),
]

SFP_TRADE_LOG = "sfp_trade_log.csv"
SFP_EXIT_LOG = "sfp_exit_log.csv"
SFP_STATE_FILE = "sfp_position_state.json"

# ──────────────────────────────────────────────
# Rate-limited API client
# ──────────────────────────────────────────────

_last_call_ts = 0.0
_call_count = 0
_rate_limit_hits = 0


def api_get(base_url, path, params=None):
    """Rate-limited GET request to Bybit public API."""
    global _last_call_ts, _call_count, _rate_limit_hits

    if _rate_limit_hits >= 3:
        time.sleep(10)
        _rate_limit_hits = 0

    elapsed = time.time() - _last_call_ts
    if elapsed < MIN_CALL_INTERVAL:
        time.sleep(MIN_CALL_INTERVAL - elapsed)

    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{base_url}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "X-Referer": "bybit-skill",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _last_call_ts = time.time()
            _call_count += 1
            data = json.loads(resp.read())
            if data.get("retCode") == 10006:
                _rate_limit_hits += 1
                time.sleep(0.5 + _rate_limit_hits * 0.5)
                return api_get(base_url, path, params)
            return data
    except urllib.error.URLError:
        return {"retCode": -1, "result": {}}


# ──────────────────────────────────────────────
# Data fetching
# ──────────────────────────────────────────────

def fetch_linear_tickers(base_url):
    """Fetch all linear perpetual tickers."""
    data = api_get(base_url, "/v5/market/tickers", {"category": "linear"})
    return data.get("result", {}).get("list", [])


def fetch_klines(base_url, symbol, interval, limit):
    """Fetch klines, returned oldest-first.
    Each: [startTime, open, high, low, close, volume, turnover]
    """
    data = api_get(base_url, "/v5/market/kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
    })
    klines = data.get("result", {}).get("list", [])
    return list(reversed(klines))  # oldest first


# ──────────────────────────────────────────────
# SFP Detection Engine
# ──────────────────────────────────────────────

class Candle:
    """Parsed candle with named fields."""
    __slots__ = ("time", "open", "high", "low", "close", "volume", "turnover", "index")

    def __init__(self, raw, index):
        self.time = int(raw[0])
        self.open = float(raw[1])
        self.high = float(raw[2])
        self.low = float(raw[3])
        self.close = float(raw[4])
        self.volume = float(raw[5])
        self.turnover = float(raw[6])
        self.index = index

    @property
    def is_green(self):
        return self.close >= self.open

    @property
    def body_top(self):
        return max(self.open, self.close)

    @property
    def body_bottom(self):
        return min(self.open, self.close)

    @property
    def upper_wick(self):
        return self.high - self.body_top

    @property
    def lower_wick(self):
        return self.body_bottom - self.low

    @property
    def body_size(self):
        return abs(self.close - self.open)


def find_swing_highs(candles, lookback):
    """
    Find swing highs: a candle whose high is higher than the highs of
    `lookback` candles on both sides.
    """
    swings = []
    for i in range(lookback, len(candles) - lookback):
        high = candles[i].high
        is_swing = True
        for j in range(1, lookback + 1):
            if candles[i - j].high >= high or candles[i + j].high >= high:
                is_swing = False
                break
        if is_swing:
            swings.append(candles[i])
    return swings


def find_swing_lows(candles, lookback):
    """
    Find swing lows: a candle whose low is lower than the lows of
    `lookback` candles on both sides.
    """
    swings = []
    for i in range(lookback, len(candles) - lookback):
        low = candles[i].low
        is_swing = True
        for j in range(1, lookback + 1):
            if candles[i - j].low <= low or candles[i + j].low <= low:
                is_swing = False
                break
        if is_swing:
            swings.append(candles[i])
    return swings


def detect_sfps(candles, lookback, min_sweep_pct, max_candles_ago):
    """
    Detect Swing Failure Patterns in the most recent candles.

    Bearish SFP: candle's HIGH exceeds a swing high, but CLOSE is below it.
    Bullish SFP: candle's LOW exceeds a swing low, but CLOSE is above it.

    Returns a list of SFP dicts.
    """
    if len(candles) < lookback * 2 + 5:
        return []

    swing_highs = find_swing_highs(candles, lookback)
    swing_lows = find_swing_lows(candles, lookback)

    sfps = []
    total = len(candles)

    # Only check the most recent `max_candles_ago` candles for SFPs
    # (the swing points can be anywhere in history)
    check_start = max(0, total - max_candles_ago)

    for i in range(check_start, total):
        c = candles[i]
        candles_ago = total - 1 - i  # 0 = current/latest candle

        # ── Bearish SFP ──
        # Find the most recent swing high BEFORE this candle
        relevant_highs = [sh for sh in swing_highs if sh.index < i]
        if relevant_highs:
            # Check against the nearest swing high(s)
            for sh in reversed(relevant_highs[-3:]):  # check last 3 swing highs
                sweep_pct = (c.high - sh.high) / sh.high * 100 if sh.high > 0 else 0

                if (c.high > sh.high                          # wick sweeps above
                    and sweep_pct >= min_sweep_pct             # meaningful sweep
                    and c.close < sh.high                      # closes back below
                    and c.close < c.open):                     # bearish candle (red)
                    sfps.append({
                        "type": "BEARISH",
                        "candle_time": c.time,
                        "candles_ago": candles_ago,
                        "swing_level": sh.high,
                        "sweep_high": c.high,
                        "close": c.close,
                        "sweep_pct": round(sweep_pct, 3),
                        "reclaim_pct": round((sh.high - c.close) / sh.high * 100, 3),
                        "wick_ratio": round(c.upper_wick / max(c.body_size, 1e-10), 2),
                        "volume": c.turnover,
                        "swing_candle_time": sh.time,
                    })
                    break  # one SFP per candle per direction

        # ── Bullish SFP ──
        relevant_lows = [sl for sl in swing_lows if sl.index < i]
        if relevant_lows:
            for sl in reversed(relevant_lows[-3:]):
                sweep_pct = (sl.low - c.low) / sl.low * 100 if sl.low > 0 else 0

                if (c.low < sl.low                            # wick sweeps below
                    and sweep_pct >= min_sweep_pct             # meaningful sweep
                    and c.close > sl.low                       # closes back above
                    and c.close > c.open):                     # bullish candle (green)
                    sfps.append({
                        "type": "BULLISH",
                        "candle_time": c.time,
                        "candles_ago": candles_ago,
                        "swing_level": sl.low,
                        "sweep_low": c.low,
                        "close": c.close,
                        "sweep_pct": round(sweep_pct, 3),
                        "reclaim_pct": round((c.close - sl.low) / sl.low * 100, 3),
                        "wick_ratio": round(c.lower_wick / max(c.body_size, 1e-10), 2),
                        "volume": c.turnover,
                        "swing_candle_time": sl.time,
                    })
                    break

    return sfps


def grade_sfp(sfp):
    """
    Grade an SFP from A (strongest) to C (weakest) based on quality metrics.

    Strong SFP characteristics:
      - High wick-to-body ratio (big rejection wick)
      - Small sweep % (just barely swept — precise liquidity grab)
      - Solid reclaim (closed well back inside range)
      - Recent (0 candles ago = forming now)
    """
    score = 0

    # Wick ratio: bigger wick relative to body = stronger rejection
    wr = sfp["wick_ratio"]
    if wr >= 3.0:
        score += 3
    elif wr >= 1.5:
        score += 2
    elif wr >= 0.8:
        score += 1

    # Sweep precision: smaller sweep = more precise liquidity grab
    sp = sfp["sweep_pct"]
    if sp < 0.2:
        score += 3
    elif sp < 0.5:
        score += 2
    elif sp < 1.0:
        score += 1

    # Reclaim: how far price closed back inside
    rp = sfp["reclaim_pct"]
    if rp >= 0.5:
        score += 2
    elif rp >= 0.2:
        score += 1

    # Recency
    ca = sfp["candles_ago"]
    if ca == 0:
        score += 2  # forming right now
    elif ca == 1:
        score += 1

    if score >= 8:
        return "A+"
    elif score >= 6:
        return "A"
    elif score >= 4:
        return "B"
    elif score >= 2:
        return "C"
    else:
        return "D"


# ──────────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────────

def run_sfp_scan(base_url, timeframes, min_volume_m, pivot_lookback,
                 min_sweep_pct, max_candles_ago):
    """Scan all qualifying coins for SFPs across timeframes."""
    global _call_count
    _call_count = 0

    print(f"\n  Fetching linear perpetual tickers...")
    tickers = fetch_linear_tickers(base_url)
    if not tickers:
        print("  Failed to fetch tickers.")
        return []

    # Filter: USDT pairs with sufficient volume
    min_turnover = min_volume_m * 1_000_000
    candidates = []
    for t in tickers:
        try:
            symbol = t["symbol"]
            turnover = float(t.get("turnover24h", 0))
            price = float(t.get("lastPrice", 0))
            change = float(t.get("price24hPcnt", 0)) * 100
        except (ValueError, TypeError, KeyError):
            continue
        if not symbol.endswith("USDT"):
            continue
        if turnover < min_turnover:
            continue
        if price <= 0:
            continue
        candidates.append({
            "symbol": symbol,
            "lastPrice": price,
            "change24h": change,
            "turnover24h": turnover,
        })

    candidates.sort(key=lambda x: x["turnover24h"], reverse=True)
    print(f"  {len(tickers)} perps total → {len(candidates)} with >${min_volume_m}M volume")
    print(f"  Scanning {len(timeframes)} timeframe(s): {', '.join(timeframes)}\n")

    all_results = []

    for i, c in enumerate(candidates):
        symbol = c["symbol"]
        pct = (i + 1) / len(candidates) * 100
        sys.stdout.write(f"\r  Scanning [{i+1}/{len(candidates)}] {symbol:<16} ({pct:.0f}%)")
        sys.stdout.flush()

        for tf_label in timeframes:
            interval, limit = TIMEFRAMES[tf_label]
            raw_klines = fetch_klines(base_url, symbol, interval, limit)
            if len(raw_klines) < pivot_lookback * 2 + 10:
                continue

            candles = [Candle(k, idx) for idx, k in enumerate(raw_klines)]
            sfps = detect_sfps(candles, pivot_lookback, min_sweep_pct, max_candles_ago)

            for sfp in sfps:
                grade = grade_sfp(sfp)
                all_results.append({
                    **c,
                    "timeframe": tf_label,
                    "grade": grade,
                    "sfp": sfp,
                })

    print(f"\r  Scan complete. {_call_count} API calls made.{' ' * 40}")
    return all_results


def display_results(results, env_label, timeframes):
    """Display SFP scan results."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Sort: grade first (A+ > A > B > C > D), then by recency
    grade_order = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    results.sort(key=lambda r: (grade_order.get(r["grade"], 5), r["sfp"]["candles_ago"]))

    print(f"\n{'='*130}")
    print(f"[{env_label}] SFP SCANNER — {now}")
    print(f"{'='*130}")

    if not results:
        print(f"\n  No Swing Failure Patterns detected on {', '.join(timeframes)}.\n")
        return

    # Split by type
    bullish = [r for r in results if r["sfp"]["type"] == "BULLISH"]
    bearish = [r for r in results if r["sfp"]["type"] == "BEARISH"]

    for label, group in [("BULLISH SFPs (potential long entries)", bullish),
                         ("BEARISH SFPs (potential short entries)", bearish)]:
        if not group:
            continue

        print(f"\n  {label}")
        print(f"  {'-'*120}")
        print(
            f"  {'#':>3}  {'Grade':<6} {'Symbol':<14} {'TF':<5} {'Price':>12} {'24h%':>8}"
            f"  {'Swing Lvl':>12} {'Swept':>8} {'Reclaim':>8} {'Wick/Body':>10}"
            f"  {'Ago':>4}  {'Volume':>14}"
        )
        print(f"  {'-'*120}")

        for i, r in enumerate(group, 1):
            sfp = r["sfp"]
            vol_str = f"${r['turnover24h']/1e6:,.1f}M"
            ago_str = "NOW" if sfp["candles_ago"] == 0 else f"{sfp['candles_ago']}b"

            print(
                f"  {i:>3}  {r['grade']:<6} {r['symbol']:<14} {r['timeframe']:<5}"
                f" {r['lastPrice']:>12,.6g} {r['change24h']:>+7.1f}%"
                f"  {sfp['swing_level']:>12,.6g} {sfp['sweep_pct']:>7.3f}%"
                f" {sfp['reclaim_pct']:>7.3f}% {sfp['wick_ratio']:>9.1f}x"
                f"  {ago_str:>4}  {vol_str:>14}"
            )

    print(f"\n  {'-'*80}")
    print(f"  GRADE KEY:")
    print(f"    A+ = Textbook SFP (big wick, precise sweep, strong reclaim, forming now)")
    print(f"    A  = High-quality SFP")
    print(f"    B  = Decent SFP (may need confirmation)")
    print(f"    C  = Marginal SFP (lower conviction)")
    print()
    print(f"  COLUMNS:")
    print(f"    Swing Lvl = the swing high/low that was swept")
    print(f"    Swept     = how far price pierced beyond the swing level")
    print(f"    Reclaim   = how far price closed back inside (higher = stronger rejection)")
    print(f"    Wick/Body = wick size vs body size (higher = stronger rejection candle)")
    print(f"    Ago       = candles ago (NOW = current candle, 1b = 1 bar ago)")
    print()

    # Detailed breakdown for top results
    top_results = results[:5]
    if top_results:
        print(f"  {'─'*60}")
        print(f"  DETAILED BREAKDOWN — Top {len(top_results)}")
        print(f"  {'─'*60}")

        for r in top_results:
            sfp = r["sfp"]
            sfp_type = sfp["type"]
            direction = "above swing high" if sfp_type == "BEARISH" else "below swing low"
            action = "SHORT" if sfp_type == "BEARISH" else "LONG"
            sweep_key = "sweep_high" if sfp_type == "BEARISH" else "sweep_low"
            sweep_price = sfp.get(sweep_key, "N/A")

            ts = datetime.fromtimestamp(sfp["candle_time"] / 1000, tz=timezone.utc)
            candle_str = ts.strftime("%Y-%m-%d %H:%M UTC")

            swing_ts = datetime.fromtimestamp(sfp["swing_candle_time"] / 1000, tz=timezone.utc)
            swing_str = swing_ts.strftime("%Y-%m-%d %H:%M UTC")

            print(f"\n  {r['grade']} | {r['symbol']} ({r['timeframe']}) — {sfp_type} SFP → potential {action}")
            print(f"    Swing level:    {sfp['swing_level']:,.6g} (formed {swing_str})")
            print(f"    Sweep candle:   {candle_str}")
            print(f"    Wick swept to:  {sweep_price:,.6g} ({direction}, {sfp['sweep_pct']:.3f}% beyond)")
            print(f"    Closed at:      {sfp['close']:,.6g} ({sfp['reclaim_pct']:.3f}% back inside)")
            print(f"    Rejection wick: {sfp['wick_ratio']:.1f}x body size")

    print()


def save_results(results, filename="sfp_scan.json"):
    """Save results to JSON."""
    with open(filename, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Raw data saved to {filename}")


# ──────────────────────────────────────────────
# Trading + Position Management
# ──────────────────────────────────────────────

GRADE_ORDER = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4}


def get_credentials():
    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        print("\n  ERROR: BYBIT_API_KEY and BYBIT_API_SECRET must be set.")
        sys.exit(1)
    return api_key, api_secret


def sign_request(api_key, api_secret, timestamp, params_str):
    sign_str = f"{timestamp}{api_key}{RECV_WINDOW}{params_str}"
    return hmac.new(api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()


def auth_request(base_url, method, path, api_key, api_secret, params=None):
    timestamp = str(int(time.time() * 1000))
    if method == "GET":
        qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        sign = sign_request(api_key, api_secret, timestamp, qs)
        url = f"{base_url}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(url, method="GET")
    else:
        body = json.dumps(params or {}, separators=(",", ":"))
        sign = sign_request(api_key, api_secret, timestamp, body)
        url = f"{base_url}{path}"
        req = urllib.request.Request(url, data=body.encode(), method="POST")
        req.add_header("Content-Type", "application/json")
    req.add_header("X-BAPI-API-KEY", api_key)
    req.add_header("X-BAPI-TIMESTAMP", timestamp)
    req.add_header("X-BAPI-SIGN", sign)
    req.add_header("X-BAPI-RECV-WINDOW", RECV_WINDOW)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("X-Referer", "bybit-skill")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"retCode": -1, "retMsg": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except urllib.error.URLError as e:
        return {"retCode": -1, "retMsg": str(e)}


def get_instrument_info(base_url, symbol):
    url = f"{base_url}/v5/market/instruments-info?category=linear&symbol={symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            items = data.get("result", {}).get("list", [])
            return items[0] if items else None
    except Exception:
        return None


def calculate_qty(amount_usdt, price, instrument):
    if price <= 0:
        return None
    lot = instrument.get("lotSizeFilter", {})
    min_qty = float(lot.get("minOrderQty", "0.001"))
    qty_step = float(lot.get("qtyStep", "0.001"))
    raw = amount_usdt / price
    if raw < min_qty:
        return None
    steps = int(raw / qty_step)
    qty = steps * qty_step
    if qty < min_qty:
        return None
    if qty_step >= 1:
        return str(int(qty))
    decimals = len(str(qty_step).rstrip("0").split(".")[-1])
    return f"{qty:.{decimals}f}"


def init_sfp_logs():
    if not Path(SFP_TRADE_LOG).exists():
        with open(SFP_TRADE_LOG, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "symbol", "side", "qty", "price", "amount_usdt",
                "leverage", "grade", "sfp_type", "timeframe", "status", "order_id", "mode",
            ])
    if not Path(SFP_EXIT_LOG).exists():
        with open(SFP_EXIT_LOG, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "symbol", "side", "entry_price",
                "profit_pct", "trailing_tier", "action",
            ])


def log_sfp_trade(symbol, side, qty, price, amount, leverage, grade, sfp_type, tf, status, oid, mode):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(SFP_TRADE_LOG, "a", newline="") as f:
        csv.writer(f).writerow([now, symbol, side, qty, price, amount, leverage, grade, sfp_type, tf, status, oid, mode])


def log_sfp_exit(symbol, side, entry_price, profit_pct, tier, action):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(SFP_EXIT_LOG, "a", newline="") as f:
        csv.writer(f).writerow([now, symbol, side, entry_price, profit_pct, tier, action])


def load_sfp_state():
    if Path(SFP_STATE_FILE).exists():
        with open(SFP_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_sfp_state(state):
    with open(SFP_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_current_tier(profit_pct):
    active = TRAILING_TIERS[0]
    for min_p, trail in TRAILING_TIERS:
        if profit_pct >= min_p:
            active = (min_p, trail)
    return active


def manage_sfp_positions(base_url, api_key, api_secret, initial_sl_pct, is_live, pos_state):
    """Check open positions and manage trailing stops."""
    data = auth_request(base_url, "GET", "/v5/position/list",
                        api_key, api_secret, {"category": "linear", "settleCoin": "USDT"})
    if data.get("retCode") != 0:
        return pos_state
    positions = [p for p in data.get("result", {}).get("list", [])
                 if float(p.get("size", "0") or "0") > 0]

    if not positions:
        if pos_state:
            pos_state = {}
            save_sfp_state(pos_state)
        return pos_state

    total_unrealised = 0.0
    print(f"\n  {'='*90}")
    print(f"  OPEN POSITIONS — {len(positions)} active")
    print(f"  {'='*90}")
    active_symbols = set()

    for pos in positions:
        symbol = pos.get("symbol", "")
        side = pos.get("side", "")
        size = float(pos.get("size", "0") or "0")
        entry_price = float(pos.get("avgPrice", "0") or "0")
        mark_price = float(pos.get("markPrice", "0") or "0")
        position_idx = int(pos.get("positionIdx", "0") or "0")
        current_sl = float(pos.get("stopLoss", "0") or "0")
        current_trail = float(pos.get("trailingStop", "0") or "0")
        leverage = pos.get("leverage", "?")
        unrealised_pnl = float(pos.get("unrealisedPnl", "0") or "0")
        position_value = float(pos.get("positionValue", "0") or "0")

        if entry_price <= 0 or mark_price <= 0:
            continue
        active_symbols.add(symbol)
        total_unrealised += unrealised_pnl

        if side == "Buy":
            profit_pct = (mark_price - entry_price) / entry_price * 100
        else:
            profit_pct = (entry_price - mark_price) / entry_price * 100

        lev = float(leverage) if leverage != "?" else 1
        _, tier_trail_pct = get_current_tier(profit_pct)
        state_key = f"{symbol}_{side}"

        if current_sl > 0:
            if side == "Buy":
                sl_dist_pct = (mark_price - current_sl) / mark_price * 100
            else:
                sl_dist_pct = (current_sl - mark_price) / mark_price * 100
            sl_str = f"${current_sl:,.6g} ({sl_dist_pct:.1f}% away)"
        else:
            sl_str = "NONE ⚠"

        trail_str = f"${current_trail:,.6g}" if current_trail > 0 else "OFF"
        tier_str = f"{tier_trail_pct}%" if tier_trail_pct > 0 else "SL only"
        pnl_color = "+" if unrealised_pnl >= 0 else ""

        print(f"\n  {symbol} {side} {lev:.0f}x")
        print(f"    Entry: ${entry_price:,.6g}  →  Now: ${mark_price:,.6g}  |  Size: {size} (~${position_value:,.2f})")
        print(f"    P&L:   {pnl_color}${unrealised_pnl:,.2f} USDT  ({profit_pct:+.2f}% / {profit_pct*lev:+.1f}% with leverage)")
        print(f"    SL:    {sl_str}  |  Trail: {trail_str}  |  Tier: {tier_str}")

        ps = pos_state.get(state_key, {"initial_sl_set": False, "current_tier_pct": 0, "highest_profit": 0})
        if profit_pct > ps.get("highest_profit", 0):
            ps["highest_profit"] = profit_pct

        action = None
        new_sl = None
        new_trail = None

        if not ps["initial_sl_set"] and current_sl == 0:
            if side == "Buy":
                new_sl = round(entry_price * (1 - initial_sl_pct / 100), 6)
            else:
                new_sl = round(entry_price * (1 + initial_sl_pct / 100), 6)
            action = f"SET initial SL at ${new_sl:,.6g} (-{initial_sl_pct}%)"
            ps["initial_sl_set"] = True
        elif tier_trail_pct > 0 and tier_trail_pct != ps.get("current_tier_pct", 0):
            if tier_trail_pct < ps.get("current_tier_pct", 999) or ps.get("current_tier_pct", 0) == 0:
                new_trail = round(mark_price * tier_trail_pct / 100, 6)
                label = "ACTIVATE" if ps.get("current_tier_pct", 0) == 0 else "TIGHTEN"
                action = f"{label} trail to {tier_trail_pct}% (${new_trail:,.6g} distance)"
                ps["current_tier_pct"] = tier_trail_pct

        if action:
            print(f"    >> {action}")
            if is_live:
                params = {"category": "linear", "symbol": symbol, "positionIdx": position_idx}
                if new_sl is not None:
                    params["stopLoss"] = str(new_sl)
                if new_trail is not None:
                    params["trailingStop"] = str(new_trail)
                result = auth_request(base_url, "POST", "/v5/position/trading-stop",
                                      api_key, api_secret, params)
                ret = result.get("retCode", -1)
                if ret == 0:
                    print(f"    >> APPLIED")
                elif ret == 10001 and "position idx" in result.get("retMsg", "").lower():
                    alt_idx = 1 if side == "Buy" else 2
                    time.sleep(0.3)
                    params["positionIdx"] = alt_idx
                    r2 = auth_request(base_url, "POST", "/v5/position/trading-stop",
                                      api_key, api_secret, params)
                    print(f"    >> {'APPLIED (hedge)' if r2.get('retCode') == 0 else 'FAILED: ' + r2.get('retMsg', '')}")
                else:
                    print(f"    >> FAILED: {result.get('retMsg')}")
                log_sfp_exit(symbol, side, entry_price, profit_pct, tier_trail_pct, action)
            else:
                print(f"    >> [DRY-RUN] Would apply")
        else:
            print(f"    >> OK")

        pos_state[state_key] = ps

    # Total P&L summary
    pnl_sign = "+" if total_unrealised >= 0 else ""
    print(f"\n  {'─'*50}")
    print(f"  TOTAL UNREALISED P&L:  {pnl_sign}${total_unrealised:,.2f} USDT")
    print(f"  {'─'*50}")

    closed = [k for k in list(pos_state.keys()) if k.split("_")[0] not in active_symbols]
    for k in closed:
        print(f"  Position closed: {k}")
        del pos_state[k]
    save_sfp_state(pos_state)
    return pos_state


def execute_sfp_trades(results, base_url, api_key, api_secret, args, traded_symbols):
    """Trade the best SFP signals. Returns updated traded_symbols set."""
    min_grade = args.min_grade.upper()
    tradeable = [r for r in results
                 if GRADE_ORDER.get(r["grade"], 99) <= GRADE_ORDER.get(min_grade, 2)
                 and r["sfp"]["candles_ago"] <= 1  # only trade fresh SFPs
                 and r["symbol"] not in traded_symbols]

    if not tradeable:
        print(f"\n  No tradeable SFPs (grade {min_grade}+ and fresh).\n")
        return traded_symbols

    trades_this_cycle = 0
    for r in tradeable:
        if trades_this_cycle >= MAX_TRADES_PER_CYCLE:
            break

        symbol = r["symbol"]
        grade = r["grade"]
        sfp = r["sfp"]
        sfp_type = sfp["type"]
        side = "Sell" if sfp_type == "BEARISH" else "Buy"
        price = r["lastPrice"]
        tf = r["timeframe"]

        print(f"  >> {sfp_type} SFP [{grade}] on {symbol} ({tf}) @ ${price:,.6g}")

        instrument = get_instrument_info(base_url, symbol)
        if not instrument:
            print(f"     SKIP: no instrument info")
            continue

        qty = calculate_qty(args.amount, price, instrument)
        if not qty:
            print(f"     SKIP: qty too small")
            continue

        est_value = float(qty) * price
        print(f"     Order: {side.upper()} {qty} {symbol} (~${est_value:,.2f}) @ {args.leverage}x")

        # Calculate stop loss price (direction-aware, set atomically with order)
        if side == "Buy":
            sl_price = round(price * (1 - args.initial_sl / 100), 6)
        else:
            sl_price = round(price * (1 + args.initial_sl / 100), 6)

        if not args.live:
            print(f"     [DRY-RUN] Would place order with SL at ${sl_price:,.6g} (-{args.initial_sl}%)")
            log_sfp_trade(symbol, side, qty, price, est_value, args.leverage,
                          grade, sfp_type, tf, "dry-run", "N/A", "dry-run")
            traded_symbols.add(symbol)
            trades_this_cycle += 1
        else:
            # Set leverage
            auth_request(base_url, "POST", "/v5/position/set-leverage",
                         api_key, api_secret, {
                             "category": "linear", "symbol": symbol,
                             "buyLeverage": str(args.leverage), "sellLeverage": str(args.leverage),
                         })
            time.sleep(0.3)

            print(f"     Placing order with SL at ${sl_price:,.6g} (-{args.initial_sl}%)...", end=" ")
            order_params = {
                "category": "linear", "symbol": symbol, "side": side,
                "orderType": "Market", "qty": qty, "positionIdx": 0,
                "orderLinkId": f"sfp_{symbol}_{int(time.time())}",
                "stopLoss": str(sl_price),
            }
            result = auth_request(base_url, "POST", "/v5/order/create",
                                  api_key, api_secret, order_params)
            ret = result.get("retCode", -1)
            oid = result.get("result", {}).get("orderId", "N/A")

            if ret == 0:
                print(f"FILLED (orderId: {oid})")
                log_sfp_trade(symbol, side, qty, price, est_value, args.leverage,
                              grade, sfp_type, tf, "filled", oid, "live")
                traded_symbols.add(symbol)
                trades_this_cycle += 1
                # Mark SL as already set so position manager doesn't re-set it
                state_key = f"{symbol}_{side}"
                pos_state = load_sfp_state()
                pos_state[state_key] = {
                    "initial_sl_set": True, "current_tier_pct": 0, "highest_profit": 0,
                }
                save_sfp_state(pos_state)
            elif ret == 10001 and "position idx" in result.get("retMsg", "").lower():
                pos_idx = 1 if side == "Buy" else 2
                order_params["positionIdx"] = pos_idx
                order_params["orderLinkId"] = f"sfp_{symbol}_{int(time.time())}"
                time.sleep(0.3)
                r2 = auth_request(base_url, "POST", "/v5/order/create",
                                  api_key, api_secret, order_params)
                if r2.get("retCode") == 0:
                    oid2 = r2.get("result", {}).get("orderId", "N/A")
                    print(f"FILLED hedge (orderId: {oid2})")
                    log_sfp_trade(symbol, side, qty, price, est_value, args.leverage,
                                  grade, sfp_type, tf, "filled", oid2, "live")
                    traded_symbols.add(symbol)
                    trades_this_cycle += 1
                    state_key = f"{symbol}_{side}"
                    pos_state = load_sfp_state()
                    pos_state[state_key] = {
                        "initial_sl_set": True, "current_tier_pct": 0, "highest_profit": 0,
                    }
                    save_sfp_state(pos_state)
                else:
                    print(f"     FAILED: {r2.get('retMsg')}")
            else:
                print(f"     FAILED: {result.get('retMsg')}")

        print()

    return traded_symbols


def main():
    parser = argparse.ArgumentParser(
        description="Bybit SFP Scanner — detect Swing Failure Patterns on high-volume coins"
    )
    parser.add_argument("--testnet", action="store_true", help="Use testnet")
    parser.add_argument("--timeframe", type=str, default=None,
                        help="Single timeframe to scan: 15m, 1h, 4h, 1d (default: 1h + 4h)")
    parser.add_argument("--min-volume", type=float, default=DEFAULT_MIN_VOLUME_M,
                        help=f"Min 24h turnover in millions USD (default: {DEFAULT_MIN_VOLUME_M})")
    parser.add_argument("--lookback", type=int, default=DEFAULT_PIVOT_LOOKBACK,
                        help=f"Pivot lookback bars (default: {DEFAULT_PIVOT_LOOKBACK})")
    parser.add_argument("--min-sweep", type=float, default=DEFAULT_MIN_SWEEP_PCT,
                        help=f"Min sweep beyond swing level in %% (default: {DEFAULT_MIN_SWEEP_PCT})")
    parser.add_argument("--max-ago", type=int, default=DEFAULT_MAX_CANDLES_AGO,
                        help=f"Only show SFPs from last N candles (default: {DEFAULT_MAX_CANDLES_AGO})")
    parser.add_argument("--watch", type=int, default=0,
                        help="Rescan interval in minutes (0 = one-shot)")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    # Trading flags
    parser.add_argument("--trade", action="store_true",
                        help="Enable auto-trading on SFP signals (dry-run by default)")
    parser.add_argument("--live", action="store_true",
                        help="Execute real trades (requires --trade)")
    parser.add_argument("--amount", type=float, default=DEFAULT_AMOUNT_USDT,
                        help=f"USDT per trade (default: {DEFAULT_AMOUNT_USDT})")
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE,
                        help=f"Leverage (default: {DEFAULT_LEVERAGE}x)")
    parser.add_argument("--initial-sl", type=float, default=DEFAULT_INITIAL_SL_PCT,
                        help=f"Initial stop loss %% (default: {DEFAULT_INITIAL_SL_PCT})")
    parser.add_argument("--min-grade", type=str, default=DEFAULT_MIN_TRADE_GRADE,
                        help=f"Min SFP grade to trade: A+, A, B, C (default: {DEFAULT_MIN_TRADE_GRADE})")
    args = parser.parse_args()

    base_url = TESTNET_URL if args.testnet else MAINNET_URL
    env_label = "TESTNET" if args.testnet else "MAINNET"

    if args.timeframe:
        if args.timeframe not in TIMEFRAMES:
            print(f"Invalid timeframe: {args.timeframe}. Choose: {', '.join(TIMEFRAMES.keys())}")
            sys.exit(1)
        timeframes = [args.timeframe]
    else:
        timeframes = DEFAULT_TIMEFRAMES

    trading_mode = args.trade
    is_live = args.live and args.trade
    mode_str = "LIVE" if is_live else ("DRY-RUN" if trading_mode else "SCAN ONLY")

    api_key = api_secret = None
    pos_state = {}
    traded_symbols = set()

    if trading_mode:
        api_key, api_secret = get_credentials()
        init_sfp_logs()
        pos_state = load_sfp_state()

        if is_live and not args.testnet:
            print(f"\n  WARNING: LIVE SFP auto-trading on MAINNET.")
            print(f"  ${args.amount} per trade | {args.leverage}x | Min grade: {args.min_grade}")
            confirm = input("\n  Type CONFIRM to proceed: ").strip()
            if confirm.upper() != "CONFIRM":
                print("  Cancelled.")
                sys.exit(0)

    print(f"\n[{env_label}] Bybit SFP Scanner [{mode_str}]")
    print(f"  Timeframes:      {', '.join(timeframes)}")
    print(f"  Min volume:      ${args.min_volume}M")
    print(f"  Pivot lookback:  {args.lookback} bars")
    print(f"  Min sweep:       {args.min_sweep}%")
    print(f"  Max candles ago: {args.max_ago}")
    if trading_mode:
        print(f"  Trade amount:    ${args.amount} @ {args.leverage}x")
        print(f"  Min grade:       {args.min_grade}")
        print(f"  Initial SL:      {args.initial_sl}%")
        print(f"  Trailing tiers:  10%→5% | 30%→3% | 100%→2% | 300%→1.5%")

    while True:
        results = run_sfp_scan(
            base_url, timeframes, args.min_volume, args.lookback,
            args.min_sweep, args.max_ago,
        )
        display_results(results, env_label, timeframes)

        # Auto-trade SFP signals
        if trading_mode and results:
            traded_symbols = execute_sfp_trades(
                results, base_url, api_key, api_secret, args, traded_symbols)

        # Manage existing positions
        if trading_mode:
            pos_state = manage_sfp_positions(
                base_url, api_key, api_secret, args.initial_sl, is_live, pos_state)

        if args.save:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            save_results(results, f"sfp_scan_{ts}.json")

        if args.watch <= 0:
            break

        if trading_mode:
            print(f"\n  Next scan in {args.watch} min (positions checked every 1 min)... (Ctrl+C to stop)\n")
        else:
            print(f"\n  Next scan in {args.watch} minutes... (Ctrl+C to stop)\n")
        try:
            remaining = args.watch * 60
            while remaining > 0:
                wait = min(60, remaining) if trading_mode else remaining
                time.sleep(wait)
                remaining -= wait
                if remaining > 0 and trading_mode:
                    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                    print(f"  [position check | {now} | next scan in {remaining//60}m{remaining%60:02d}s]")
                    pos_state = manage_sfp_positions(
                        base_url, api_key, api_secret, args.initial_sl, is_live, pos_state)
        except KeyboardInterrupt:
            print("\n  Scanner stopped.")
            break


if __name__ == "__main__":
    main()
