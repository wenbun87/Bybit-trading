#!/usr/bin/env python3
"""
Bybit SFP Scanner V2 + Auto-Trader + Position Manager

Multi-timeframe Swing Failure Pattern detection matching TradingView's
"SFP Scanner V2" indicator:

  1. Pivots on a higher timeframe (default 4H, L15/R15)
  2. Breakout/reclaim detection on a lower counting timeframe (default 15m)
     - Min/max bars closed outside before reclaim
     - min_bars=0 enables wick-only sweeps
  3. Optional EMA/SMA trend filter (default: EMA 50/200 on pivot TF)
  4. Optional structural filter (rising/falling pivot direction)
  5. Anti-spam: one-shot per pivot level

Trading: shares trailing-stop tiers with auto_trader.py and supports
zero-hero mode (--no-stops) and 6h re-entry cooldown.

Usage:
    python3 sfp_scanner.py                              # scan only
    python3 sfp_scanner.py --watch 30                   # rescan every 30 min
    python3 sfp_scanner.py --trade                      # dry-run auto-trading
    python3 sfp_scanner.py --trade --live --amount 500
    python3 sfp_scanner.py --trade --no-stops --amount 250  # zero-hero
    python3 sfp_scanner.py --pivot-tf 240 --count-tf 15     # 4H pivots, 15m counting
    python3 sfp_scanner.py --no-ma-filter                   # disable MA trend filter
    python3 sfp_scanner.py --struct-filter --struct-count 3 # enable structural filter

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

import shared_state

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
USER_AGENT = "bybit-skill/1.2.3"
MIN_CALL_INTERVAL = 0.12  # 120ms between GET requests

# SFP V2 detection parameters (matching TradingView SFP Scanner V2 indicator)
DEFAULT_PIVOT_TF = "60"           # 1H — pivot computation timeframe
DEFAULT_COUNT_TF = "5"            # 5m — breakout/reclaim + MSB/breaker detection timeframe
DEFAULT_PIVOT_LEFT = 15           # left bars for pivot confirmation
DEFAULT_PIVOT_RIGHT = 15          # right bars for pivot confirmation
DEFAULT_PIVOT_SOURCE = "wicks"    # "wicks" or "closes"
DEFAULT_MIN_BARS_BREAKOUT = 1     # min counting-TF bars closed outside before reclaim (0 = wick-only)
DEFAULT_MAX_BARS_BREAKOUT = 30    # max counting-TF bars before reclaim
DEFAULT_LEVELS_TO_SCAN = 2        # recent pivot levels per side
DEFAULT_MIN_VOLUME_M = 2          # min 24h turnover in millions USD

# MA trend filter
DEFAULT_MA_FILTER = True
DEFAULT_MA_TF = "60"
DEFAULT_MA_TYPE = "EMA"
DEFAULT_MA_FAST = 50
DEFAULT_MA_SLOW = 200

# Structural filter
DEFAULT_STRUCT_FILTER = False
DEFAULT_STRUCT_COUNT = 2

# MSB + Breaker Block confirmation (on count_tf candles)
DEFAULT_MSB_FILTER = True         # require market structure break to confirm SFP
DEFAULT_MSB_SWING_LEFT = 3        # left bars for M5 swing point detection
DEFAULT_MSB_SWING_RIGHT = 3       # right bars for M5 swing point detection
DEFAULT_MSB_LOOKBACK = 50         # how many count_tf bars after SFP to look for MSB

INTERVAL_LABELS = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "1h", "120": "2h", "240": "4h", "360": "6h", "720": "12h",
    "D": "1D", "W": "1W", "M": "1M",
}

# Trading config
RECV_WINDOW = "5000"
DEFAULT_AMOUNT_USDT = 500
DEFAULT_ACCOUNT_BALANCE = 500
DEFAULT_MAX_EXPOSURE_MULT = 5
DEFAULT_LEVERAGE = 5
MAX_TRADES_PER_CYCLE = 2
MAX_TRADES_PER_DAY = 6
DEFAULT_INITIAL_SL_PCT = 8.0
DEFAULT_MIN_TRADE_GRADE = "B"
RE_ENTRY_COOLDOWN_HOURS = 6

TRAILING_TIERS = [
    (0,    0),
    (10,   8.0),
    (30,   6.0),
    (100,  3.0),
    (300,  2.0),
]

SFP_TRADE_LOG = "sfp_trade_log.csv"
SFP_EXIT_LOG = "sfp_exit_log.csv"
SFP_STATE_FILE = "sfp_position_state.json"

# Paper trading defaults
PAPER_AMOUNT_USDT = 500
PAPER_LEVERAGE = 10
SL_BUFFER_PCT = 1.0    # 1.0% beyond sweep price for stop loss (0.15% was too tight for crypto volatility)

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
    last_data: dict = {"retCode": -1, "result": {}}
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                _last_call_ts = time.time()
                _call_count += 1
                data = json.loads(resp.read())
                if data.get("retCode") == 10006:
                    _rate_limit_hits += 1
                    time.sleep(0.5 + _rate_limit_hits * 0.5)
                    last_data = data
                    continue
                return data
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"retCode": -1, "result": {}}
    return last_data


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


def compute_pivots(candles, left, right, source="wicks"):
    """Find pivot highs (resistances) and pivot lows (supports).

    Returns (pivot_highs, pivot_lows) where each is a list of
    (candle_index, level_price, candle_time).
    """
    pivot_highs = []
    pivot_lows = []
    use_close = (source == "closes")

    for i in range(left, len(candles) - right):
        val_high = candles[i].close if use_close else candles[i].high
        val_low = candles[i].close if use_close else candles[i].low

        is_high = True
        for j in range(1, left + 1):
            cmp = candles[i - j].close if use_close else candles[i - j].high
            if cmp >= val_high:
                is_high = False
                break
        if is_high:
            for j in range(1, right + 1):
                cmp = candles[i + j].close if use_close else candles[i + j].high
                if cmp >= val_high:
                    is_high = False
                    break
        if is_high:
            pivot_highs.append((i, val_high, candles[i].time))

        is_low = True
        for j in range(1, left + 1):
            cmp = candles[i - j].close if use_close else candles[i - j].low
            if cmp <= val_low:
                is_low = False
                break
        if is_low:
            for j in range(1, right + 1):
                cmp = candles[i + j].close if use_close else candles[i + j].low
                if cmp <= val_low:
                    is_low = False
                    break
        if is_low:
            pivot_lows.append((i, val_low, candles[i].time))

    return pivot_highs, pivot_lows


def compute_ema(values, length):
    if len(values) < length:
        return [None] * len(values)
    k = 2.0 / (length + 1)
    ema = [None] * (length - 1)
    ema.append(sum(values[:length]) / length)
    for i in range(length, len(values)):
        ema.append(values[i] * k + ema[-1] * (1 - k))
    return ema


def compute_sma(values, length):
    if len(values) < length:
        return [None] * len(values)
    sma = [None] * (length - 1)
    s = sum(values[:length])
    sma.append(s / length)
    for i in range(length, len(values)):
        s += values[i] - values[i - length]
        sma.append(s / length)
    return sma


def compute_ma(values, length, ma_type="EMA"):
    return compute_ema(values, length) if ma_type.upper() == "EMA" else compute_sma(values, length)


def get_trend_direction(candles, fast_len, slow_len, ma_type="EMA"):
    closes = [c.close for c in candles]
    fast = compute_ma(closes, fast_len, ma_type)
    slow = compute_ma(closes, slow_len, ma_type)
    if fast[-1] is None or slow[-1] is None:
        return "neutral", None, None
    return ("bullish" if fast[-1] > slow[-1] else "bearish", fast[-1], slow[-1])


def check_structure(pivot_highs, pivot_lows, count):
    res = {"supports_rising": False, "supports_falling": False,
           "resistances_rising": False, "resistances_falling": False}
    if len(pivot_highs) >= count:
        recent = [ph[1] for ph in pivot_highs[-count:]]
        res["resistances_rising"] = all(recent[i] > recent[i-1] for i in range(1, len(recent)))
        res["resistances_falling"] = all(recent[i] < recent[i-1] for i in range(1, len(recent)))
    if len(pivot_lows) >= count:
        recent = [pl[1] for pl in pivot_lows[-count:]]
        res["supports_rising"] = all(recent[i] > recent[i-1] for i in range(1, len(recent)))
        res["supports_falling"] = all(recent[i] < recent[i-1] for i in range(1, len(recent)))
    return res


def find_micro_swings(candles, left, right):
    """Find swing highs and lows on count-TF candles (small L/R for M5 structure).

    Returns (swing_highs, swing_lows) — each is list of (index, price).
    """
    swing_highs = []
    swing_lows = []

    for i in range(left, len(candles) - right):
        h = candles[i].high
        l = candles[i].low

        is_high = all(candles[i - j].high <= h for j in range(1, left + 1)) and \
                  all(candles[i + j].high <= h for j in range(1, right + 1))
        is_low = all(candles[i - j].low >= l for j in range(1, left + 1)) and \
                 all(candles[i + j].low >= l for j in range(1, right + 1))

        if is_high:
            swing_highs.append((i, h))
        if is_low:
            swing_lows.append((i, l))

    return swing_highs, swing_lows


def detect_msb(candles, swing_highs, swing_lows, sfp_candle_idx, direction, lookback):
    """Detect a Market Structure Break after the SFP signal candle.

    For bullish SFP: look for a candle that closes above the most recent M5 swing high
    For bearish SFP: look for a candle that closes below the most recent M5 swing low

    Returns (msb_candle_idx, msb_level) or (None, None).
    """
    search_start = sfp_candle_idx + 1
    search_end = min(sfp_candle_idx + lookback, len(candles))

    if direction == "BULLISH":
        # Find the most recent swing high before/at the SFP candle
        relevant = [(i, p) for i, p in swing_highs if i <= sfp_candle_idx]
        if not relevant:
            return None, None
        _, level = relevant[-1]
        for ci in range(search_start, search_end):
            if candles[ci].close > level:
                return ci, level
    else:
        relevant = [(i, p) for i, p in swing_lows if i <= sfp_candle_idx]
        if not relevant:
            return None, None
        _, level = relevant[-1]
        for ci in range(search_start, search_end):
            if candles[ci].close < level:
                return ci, level

    return None, None


def detect_breaker_block(candles, msb_candle_idx, sfp_candle_idx, direction):
    """Find the breaker block: last opposite candle before the MSB.

    Bullish: last red (bearish) candle between SFP and MSB — its high/low zone is the breaker
    Bearish: last green (bullish) candle between SFP and MSB

    Returns dict with breaker block info or None.
    """
    search_start = max(sfp_candle_idx, 0)

    if direction == "BULLISH":
        for ci in range(msb_candle_idx - 1, search_start - 1, -1):
            c = candles[ci]
            if not c.is_green:  # bearish candle
                return {
                    "index": ci,
                    "high": c.high,
                    "low": c.low,
                    "open": c.open,
                    "close": c.close,
                    "time": c.time,
                }
    else:
        for ci in range(msb_candle_idx - 1, search_start - 1, -1):
            c = candles[ci]
            if c.is_green:  # bullish candle
                return {
                    "index": ci,
                    "high": c.high,
                    "low": c.low,
                    "open": c.open,
                    "close": c.close,
                    "time": c.time,
                }

    return None


def confirm_sfp_msb_bb(candles, sfp, swing_highs, swing_lows, msb_lookback):
    """Run MSB + breaker block confirmation on an SFP signal.

    Returns dict with msb/bb info if confirmed, else None.
    """
    direction = sfp["type"]
    total = len(candles)
    sfp_candle_idx = total - 1 - sfp["candles_ago"]

    msb_idx, msb_level = detect_msb(candles, swing_highs, swing_lows,
                                     sfp_candle_idx, direction, msb_lookback)
    if msb_idx is None:
        return None

    bb = detect_breaker_block(candles, msb_idx, sfp_candle_idx, direction)

    return {
        "msb_confirmed": True,
        "msb_candle_idx": msb_idx,
        "msb_level": msb_level,
        "msb_time": candles[msb_idx].time,
        "msb_bars_after_sfp": msb_idx - sfp_candle_idx,
        "breaker_block": bb,
    }


def detect_sfps_v2(pivot_levels, count_candles, min_bars, max_bars, consumed_levels):
    """Detect SFPs via breakout/reclaim on counting-TF candles.

    pivot_levels: list of (level_price, "resistance"|"support", level_time)
    consumed_levels: set of level keys to skip (anti-spam, modified in place)
    Returns list of SFP dicts.
    """
    sfps = []
    total = len(count_candles)

    for level_price, level_type, level_time in pivot_levels:
        level_key = f"{level_type}_{level_price}"
        if level_key in consumed_levels:
            continue

        breakout_start = None
        bars_outside = 0
        is_resistance = (level_type == "resistance")

        for ci, c in enumerate(count_candles):
            if is_resistance:
                outside = c.close > level_price
                reclaim = c.close <= level_price
                wick_sweep = c.high > level_price and c.close <= level_price
            else:
                outside = c.close < level_price
                reclaim = c.close >= level_price
                wick_sweep = c.low < level_price and c.close >= level_price

            if breakout_start is None:
                if outside:
                    breakout_start = ci
                    bars_outside = 1
                elif min_bars == 0 and wick_sweep:
                    if is_resistance:
                        sweep_price = c.high
                        sweep_pct = (c.high - level_price) / level_price * 100 if level_price > 0 else 0
                        reclaim_pct = (level_price - c.close) / level_price * 100 if level_price > 0 else 0
                        wick_ratio = c.upper_wick / max(c.body_size, 1e-10)
                    else:
                        sweep_price = c.low
                        sweep_pct = (level_price - c.low) / level_price * 100 if level_price > 0 else 0
                        reclaim_pct = (c.close - level_price) / level_price * 100 if level_price > 0 else 0
                        wick_ratio = c.lower_wick / max(c.body_size, 1e-10)
                    if sweep_pct <= 0:
                        continue
                    sfps.append({
                        "type": "BEARISH" if is_resistance else "BULLISH",
                        "candle_time": c.time, "candles_ago": total - 1 - ci,
                        "swing_level": level_price, "level_type": level_type, "level_time": level_time,
                        "sweep_price": sweep_price, "close": c.close,
                        "sweep_pct": round(sweep_pct, 3), "reclaim_pct": round(reclaim_pct, 3),
                        "bars_outside": 0, "wick_ratio": round(wick_ratio, 2), "volume": c.turnover,
                    })
                    consumed_levels.add(level_key)
                    break
            else:
                if outside:
                    bars_outside += 1
                    if bars_outside > max_bars:
                        breakout_start = None
                        bars_outside = 0
                elif reclaim:
                    if bars_outside >= min_bars and bars_outside <= max_bars:
                        if is_resistance:
                            peak = max(count_candles[j].high for j in range(breakout_start, ci + 1))
                            sweep_pct = (peak - level_price) / level_price * 100 if level_price > 0 else 0
                            reclaim_pct = (level_price - c.close) / level_price * 100 if level_price > 0 else 0
                            wick_ratio = c.upper_wick / max(c.body_size, 1e-10)
                        else:
                            peak = min(count_candles[j].low for j in range(breakout_start, ci + 1))
                            sweep_pct = (level_price - peak) / level_price * 100 if level_price > 0 else 0
                            reclaim_pct = (c.close - level_price) / level_price * 100 if level_price > 0 else 0
                            wick_ratio = c.lower_wick / max(c.body_size, 1e-10)
                        sfps.append({
                            "type": "BEARISH" if is_resistance else "BULLISH",
                            "candle_time": c.time, "candles_ago": total - 1 - ci,
                            "swing_level": level_price, "level_type": level_type, "level_time": level_time,
                            "sweep_price": peak, "close": c.close,
                            "sweep_pct": round(sweep_pct, 3), "reclaim_pct": round(abs(reclaim_pct), 3),
                            "bars_outside": bars_outside, "wick_ratio": round(wick_ratio, 2),
                            "volume": c.turnover,
                        })
                        consumed_levels.add(level_key)
                        break
                    breakout_start = None
                    bars_outside = 0

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

    # Breakout duration: longer false breakout = stronger trap
    bo = sfp.get("bars_outside", 0)
    if bo >= 3:
        score += 2
    elif bo >= 1:
        score += 1

    # Recency
    ca = sfp["candles_ago"]
    if ca <= 2:
        score += 2
    elif ca <= 5:
        score += 1

    if score >= 9:
        return "A+"
    elif score >= 7:
        return "A"
    elif score >= 5:
        return "B"
    elif score >= 3:
        return "C"
    else:
        return "D"


# ──────────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────────

def run_sfp_scan(base_url, args, consumed_levels):
    """Multi-timeframe SFP scan: pivots on pivot_tf, breakout/reclaim on count_tf."""
    global _call_count
    _call_count = 0

    print(f"\n  Fetching linear perpetual tickers...")
    tickers = fetch_linear_tickers(base_url)
    if not tickers:
        print("  Failed to fetch tickers.")
        return []

    min_turnover = args.min_volume * 1_000_000
    candidates = []
    all_usdt_turnovers = []
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
        all_usdt_turnovers.append((symbol, turnover))
        if turnover < min_turnover or price <= 0:
            continue
        candidates.append({"symbol": symbol, "lastPrice": price,
                           "change24h": change, "turnover24h": turnover})

    candidates.sort(key=lambda x: x["turnover24h"], reverse=True)

    # Debug: show turnover distribution so we can verify API data
    all_usdt_turnovers.sort(key=lambda x: x[1], reverse=True)
    n_usdt = len(all_usdt_turnovers)
    n_above_10m = sum(1 for _, tv in all_usdt_turnovers if tv >= 10_000_000)
    n_above_2m = sum(1 for _, tv in all_usdt_turnovers if tv >= 2_000_000)
    n_above_1m = sum(1 for _, tv in all_usdt_turnovers if tv >= 1_000_000)
    n_zero = sum(1 for _, tv in all_usdt_turnovers if tv == 0)
    print(f"  {len(tickers)} perps total, {n_usdt} USDT pairs")
    print(f"  Turnover: {n_above_10m} >$10M | {n_above_2m} >$2M | {n_above_1m} >$1M | {n_zero} zero")
    if all_usdt_turnovers:
        top3 = all_usdt_turnovers[:3]
        print(f"  Top 3: {', '.join(f'{s} ${tv/1e6:.0f}M' for s, tv in top3)}")

    pivot_label = INTERVAL_LABELS.get(args.pivot_tf, args.pivot_tf)
    count_label = INTERVAL_LABELS.get(args.count_tf, args.count_tf)
    print(f"  {len(candidates)} pass >${args.min_volume}M filter")
    print(f"  Pivots on {pivot_label} (L{args.pivot_left}/R{args.pivot_right}, {args.pivot_source})")
    print(f"  Count on {count_label} | levels {args.levels_to_scan} | breakout {args.min_bars}-{args.max_bars} bars")
    if args.ma_filter:
        print(f"  MA filter: {args.ma_type} {args.ma_fast}/{args.ma_slow} on {INTERVAL_LABELS.get(args.ma_tf, args.ma_tf)}")
    if args.struct_filter:
        print(f"  Struct filter: N={args.struct_count}")
    if args.msb_filter:
        print(f"  MSB + BB: swing L{args.msb_swing_left}/R{args.msb_swing_right}, lookback {args.msb_lookback} bars")
    print()

    all_results = []
    pivot_limit = min(max(args.pivot_left + args.pivot_right + 50, args.ma_slow + 50), 200)

    for i, c in enumerate(candidates):
        symbol = c["symbol"]
        pct = (i + 1) / len(candidates) * 100
        sys.stdout.write(f"\r  Scanning [{i+1}/{len(candidates)}] {symbol:<16} ({pct:.0f}%)")
        sys.stdout.flush()

        pivot_klines = fetch_klines(base_url, symbol, args.pivot_tf, pivot_limit)
        if len(pivot_klines) < args.pivot_left + args.pivot_right + 5:
            continue
        pivot_candles = [Candle(k, idx) for idx, k in enumerate(pivot_klines)]

        pivot_highs, pivot_lows = compute_pivots(
            pivot_candles, args.pivot_left, args.pivot_right, args.pivot_source)
        if not pivot_highs and not pivot_lows:
            continue

        trend, ma_fast_val, ma_slow_val = "neutral", None, None
        if args.ma_filter:
            ma_candles = pivot_candles if args.ma_tf == args.pivot_tf else \
                [Candle(k, idx) for idx, k in enumerate(fetch_klines(base_url, symbol, args.ma_tf, 200))]
            trend, ma_fast_val, ma_slow_val = get_trend_direction(
                ma_candles, args.ma_fast, args.ma_slow, args.ma_type)

        struct = check_structure(pivot_highs, pivot_lows, args.struct_count) if args.struct_filter else None

        levels = []
        for _, p, t in pivot_highs[-args.levels_to_scan:]:
            levels.append((p, "resistance", t))
        for _, p, t in pivot_lows[-args.levels_to_scan:]:
            levels.append((p, "support", t))
        if not levels:
            continue

        count_klines = fetch_klines(base_url, symbol, args.count_tf, 200)
        if len(count_klines) < 10:
            continue
        count_candles = [Candle(k, idx) for idx, k in enumerate(count_klines)]

        symbol_consumed = consumed_levels.setdefault(symbol, set())
        sfps = detect_sfps_v2(levels, count_candles, args.min_bars, args.max_bars, symbol_consumed)

        # Pre-compute micro swings for MSB detection (once per symbol)
        micro_highs, micro_lows = None, None
        if args.msb_filter and sfps:
            micro_highs, micro_lows = find_micro_swings(
                count_candles, args.msb_swing_left, args.msb_swing_right)

        for sfp in sfps:
            if args.ma_filter and trend != "neutral":
                if sfp["type"] == "BULLISH" and trend == "bearish":
                    continue
                if sfp["type"] == "BEARISH" and trend == "bullish":
                    continue
            if args.struct_filter and struct:
                if sfp["type"] == "BULLISH" and not (struct["supports_rising"] or struct["resistances_rising"]):
                    continue
                if sfp["type"] == "BEARISH" and not (struct["resistances_falling"] or struct["supports_falling"]):
                    continue

            # MSB + breaker block confirmation
            msb_info = None
            if args.msb_filter:
                msb_info = confirm_sfp_msb_bb(
                    count_candles, sfp, micro_highs, micro_lows, args.msb_lookback)
                if msb_info is None:
                    continue  # no MSB = skip this SFP

            all_results.append({
                **c, "grade": grade_sfp(sfp), "sfp": sfp,
                "trend": trend, "ma_fast": ma_fast_val, "ma_slow": ma_slow_val,
                "pivot_tf": pivot_label, "count_tf": count_label,
                "msb": msb_info,
            })

    print(f"\r  Scan complete. {_call_count} API calls made.{' ' * 40}")
    return all_results


def display_results(results, env_label, args):
    """Display SFP V2 scan results."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pivot_label = INTERVAL_LABELS.get(args.pivot_tf, args.pivot_tf)
    count_label = INTERVAL_LABELS.get(args.count_tf, args.count_tf)

    grade_order = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    results.sort(key=lambda r: (grade_order.get(r["grade"], 5), r["sfp"]["candles_ago"]))

    print(f"\n{'='*130}")
    print(f"[{env_label}] SFP SCANNER V2 — {now}")
    print(f"  Pivots: {pivot_label} ({args.pivot_source}, L{args.pivot_left}/R{args.pivot_right}) | "
          f"Count: {count_label} ({args.min_bars}-{args.max_bars} bars)")
    if args.ma_filter:
        print(f"  MA filter: {args.ma_type} {args.ma_fast}/{args.ma_slow}")
    print(f"{'='*130}")

    if not results:
        print(f"\n  No Swing Failure Patterns detected.\n")
        return

    bullish = [r for r in results if r["sfp"]["type"] == "BULLISH"]
    bearish = [r for r in results if r["sfp"]["type"] == "BEARISH"]

    for label, group in [("BULLISH SFPs (potential longs)", bullish),
                         ("BEARISH SFPs (potential shorts)", bearish)]:
        if not group:
            continue
        print(f"\n  {label}")
        print(f"  {'-'*140}")
        print(
            f"  {'#':>3}  {'Grade':<6} {'Symbol':<14} {'Price':>12} {'24h%':>8}"
            f"  {'Level':>12} {'Swept':>8} {'Bars':>5} {'Reclaim':>8} {'Wick/Bdy':>9}"
            f"  {'Ago':>4}  {'Trend':>5}  {'MSB':>4} {'BB':>3}  {'Volume':>12}"
        )
        print(f"  {'-'*140}")

        for i, r in enumerate(group, 1):
            sfp = r["sfp"]
            vol_str = f"${r['turnover24h']/1e6:,.1f}M"
            ago_str = "NOW" if sfp["candles_ago"] == 0 else f"{sfp['candles_ago']}b"
            bars_str = f"{sfp['bars_outside']}b" if sfp["bars_outside"] > 0 else "wick"
            trend_str = (r.get("trend") or "?")[:5]
            msb = r.get("msb")
            msb_str = f"{msb['msb_bars_after_sfp']}b" if msb else "—"
            bb_str = "Y" if msb and msb.get("breaker_block") else "—"

            print(
                f"  {i:>3}  {r['grade']:<6} {r['symbol']:<14}"
                f" {r['lastPrice']:>12,.6g} {r['change24h']:>+7.1f}%"
                f"  {sfp['swing_level']:>12,.6g} {sfp['sweep_pct']:>7.3f}%"
                f" {bars_str:>5} {sfp['reclaim_pct']:>7.3f}% {sfp['wick_ratio']:>8.1f}x"
                f"  {ago_str:>4}  {trend_str:>5}  {msb_str:>4} {bb_str:>3}  {vol_str:>12}"
            )

    print(f"\n  {'-'*80}")
    print(f"  GRADE KEY:")
    print(f"    A+ = Textbook SFP (big wick, precise sweep, multi-bar trap, fresh)")
    print(f"    A  = High-quality SFP")
    print(f"    B  = Decent SFP (may need confirmation)")
    print(f"    C  = Marginal SFP (lower conviction)")
    print()
    print(f"  COLUMNS:")
    print(f"    Level     = pivot level on {pivot_label} that triggered the SFP")
    print(f"    Swept     = peak distance beyond the level")
    print(f"    Bars      = counting-TF bars closed outside (or 'wick' for wick-only)")
    print(f"    Reclaim   = how far price closed back inside (higher = stronger rejection)")
    print(f"    Trend     = MA trend on {pivot_label}")
    print(f"    MSB       = market structure break confirmed (bars after SFP)")
    print(f"    BB        = breaker block found (Y/N)")
    print()

    top_results = results[:5]
    if top_results:
        print(f"  {'─'*60}")
        print(f"  DETAILED BREAKDOWN — Top {len(top_results)}")
        print(f"  {'─'*60}")

        for r in top_results:
            sfp = r["sfp"]
            sfp_type = sfp["type"]
            action = "SHORT" if sfp_type == "BEARISH" else "LONG"
            level_type = sfp.get("level_type", "?")

            ts = datetime.fromtimestamp(sfp["candle_time"] / 1000, tz=timezone.utc)
            candle_str = ts.strftime("%Y-%m-%d %H:%M UTC")
            level_ts = datetime.fromtimestamp(sfp["level_time"] / 1000, tz=timezone.utc)
            level_str = level_ts.strftime("%Y-%m-%d %H:%M UTC")
            bars_desc = f"{sfp['bars_outside']} bar(s) outside" if sfp["bars_outside"] > 0 else "wick-only sweep"

            print(f"\n  {r['grade']} | {r['symbol']} — {sfp_type} SFP → potential {action}")
            print(f"    Pivot level:    {sfp['swing_level']:,.6g} ({level_type} on {pivot_label}, formed {level_str})")
            print(f"    Signal candle:  {candle_str} ({count_label})")
            print(f"    Swept to:       {sfp['sweep_price']:,.6g} ({sfp['sweep_pct']:.3f}% beyond)")
            print(f"    Reclaimed at:   {sfp['close']:,.6g} ({sfp['reclaim_pct']:.3f}% back inside)")
            print(f"    Breakout:       {bars_desc}")
            print(f"    Wick ratio:     {sfp['wick_ratio']:.1f}x body size")
            if r.get("trend") and r["trend"] != "neutral":
                fast = r.get("ma_fast")
                slow = r.get("ma_slow")
                if fast is not None and slow is not None:
                    print(f"    Trend:          {r['trend']} ({args.ma_type} {args.ma_fast}: {fast:,.4g} / {args.ma_slow}: {slow:,.4g})")
            msb = r.get("msb")
            if msb:
                msb_ts = datetime.fromtimestamp(msb["msb_time"] / 1000, tz=timezone.utc)
                msb_str = msb_ts.strftime("%Y-%m-%d %H:%M UTC")
                print(f"    MSB:            {sfp_type.lower()} break at {msb['msb_level']:,.6g} — {msb['msb_bars_after_sfp']} bars after SFP ({msb_str})")
                bb = msb.get("breaker_block")
                if bb:
                    bb_ts = datetime.fromtimestamp(bb["time"] / 1000, tz=timezone.utc)
                    bb_str = bb_ts.strftime("%H:%M")
                    bb_type = "bearish" if sfp_type == "BULLISH" else "bullish"
                    print(f"    Breaker block:  {bb_type} candle at {bb_str} — zone {bb['low']:,.6g} to {bb['high']:,.6g}")

    print()


def save_results(results, filename="sfp_scan.json"):
    """Save results to JSON."""
    with open(filename, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Raw data saved to {filename}")


# ──────────────────────────────────────────────
# Paper Trading (Simulation)
# ──────────────────────────────────────────────

GRADE_ORDER = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4}


class PaperTrader:
    """Tracks hypothetical trades during scan-only mode."""

    def __init__(self, amount_usdt, leverage, min_grade="B",
                 account_balance=500, max_exposure_mult=5):
        self.amount = amount_usdt
        self.leverage = leverage
        self.min_grade = min_grade
        self.account_balance = account_balance
        self.max_exposure = account_balance * max_exposure_mult
        self.positions = {}       # symbol -> position dict
        self.closed_trades = []   # list of completed trade dicts
        self.traded_symbols = {}  # symbol -> last_trade_unix_ts (cooldown)
        self.start_time = time.time()

    def _get_trailing_pct(self, profit_pct):
        trail = 0
        for min_profit, trail_pct in TRAILING_TIERS:
            if profit_pct >= min_profit:
                trail = trail_pct
        return trail

    def _format_elapsed(self, seconds):
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds/60)}m"
        if seconds < 86400:
            h = int(seconds / 3600)
            m = int((seconds % 3600) / 60)
            return f"{h}h{m}m" if m else f"{h}h"
        d = int(seconds / 86400)
        h = int((seconds % 86400) / 3600)
        return f"{d}d{h}h" if h else f"{d}d"

    def enter_signals(self, results):
        """Open paper positions on qualifying signals with structural stops."""
        grade_ok = GRADE_ORDER.get(self.min_grade, 2)
        entered = 0
        now = time.time()

        for r in results:
            symbol = r["symbol"]
            if GRADE_ORDER.get(r["grade"], 99) > grade_ok:
                continue
            if symbol in self.positions:
                continue
            last_ts = self.traded_symbols.get(symbol, 0)
            if now - last_ts < RE_ENTRY_COOLDOWN_HOURS * 3600:
                continue

            current_exposure = sum(
                p["qty"] * p["entry_price"] for p in self.positions.values()
            )
            notional = self.amount
            remaining = max(0, self.max_exposure - current_exposure)
            if notional > remaining:
                notional = remaining
            if notional < 5:
                continue

            sfp = r["sfp"]
            side = "long" if sfp["type"] == "BULLISH" else "short"
            entry_price = r["lastPrice"]
            sweep_price = sfp["sweep_price"]
            qty = notional / entry_price

            # Structural stop: just beyond the sweep price (pattern invalidation)
            if side == "long":
                sl_price = sweep_price * (1 - SL_BUFFER_PCT / 100)
            else:
                sl_price = sweep_price * (1 + SL_BUFFER_PCT / 100)
            sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100

            self.positions[symbol] = {
                "side": side,
                "entry_price": entry_price,
                "qty": qty,
                "entry_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "entry_unix": time.time(),
                "grade": r["grade"],
                "sl_price": sl_price,
                "sweep_price": sweep_price,
                "trail_high": entry_price if side == "long" else None,
                "trail_low": entry_price if side == "short" else None,
            }
            self.traded_symbols[symbol] = now
            entered += 1
            action = "LONG" if side == "long" else "SHORT"
            print(f"  [PAPER] {action} {symbol} @ {entry_price:,.6g} | "
                  f"Grade {r['grade']} | ${notional:.0f} x{self.leverage} | "
                  f"SL: {sl_price:,.4g} ({sl_dist_pct:.1f}% away, beyond sweep {sweep_price:,.4g})")

        return entered

    def update_prices(self, tickers):
        """Update positions with latest prices; close if stopped out."""
        price_map = {}
        for t in tickers:
            try:
                price_map[t["symbol"]] = float(t["lastPrice"])
            except (KeyError, ValueError, TypeError):
                continue

        to_close = []
        for symbol, pos in self.positions.items():
            price = price_map.get(symbol)
            if price is None:
                continue

            side = pos["side"]
            entry = pos["entry_price"]

            if side == "long":
                pnl_pct = (price - entry) / entry * 100
                if pos["trail_high"] is None or price > pos["trail_high"]:
                    pos["trail_high"] = price
                peak = pos["trail_high"]
                peak_pnl = (peak - entry) / entry * 100
            else:
                pnl_pct = (entry - price) / entry * 100
                if pos["trail_low"] is None or price < pos["trail_low"]:
                    pos["trail_low"] = price
                peak = pos["trail_low"]
                peak_pnl = (entry - peak) / entry * 100

            # Check initial SL
            if pos["sl_price"]:
                if side == "long" and price <= pos["sl_price"]:
                    to_close.append((symbol, price, pnl_pct, "initial SL"))
                    continue
                if side == "short" and price >= pos["sl_price"]:
                    to_close.append((symbol, price, pnl_pct, "initial SL"))
                    continue

            # Check trailing stop
            trail_pct = self._get_trailing_pct(peak_pnl)
            if trail_pct > 0:
                drawdown = peak_pnl - pnl_pct
                if drawdown >= trail_pct:
                    to_close.append((symbol, price, pnl_pct, f"trailing stop ({trail_pct}%)"))

        for symbol, price, pnl_pct, reason in to_close:
            pos = self.positions.pop(symbol)
            notional = pos.get("qty", self.amount / pos["entry_price"]) * pos["entry_price"]
            pnl_usd = pnl_pct / 100 * notional
            self.closed_trades.append({
                "symbol": symbol,
                "side": pos["side"],
                "grade": pos["grade"],
                "entry_price": pos["entry_price"],
                "exit_price": price,
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "size_usdt": notional,
                "reason": reason,
                "entry_time": pos["entry_time"],
                "entry_unix": pos.get("entry_unix", time.time()),
                "exit_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "exit_unix": time.time(),
            })
            tag = "+" if pnl_usd >= 0 else ""
            print(f"  [PAPER EXIT] {symbol} | {reason} | "
                  f"{tag}${pnl_usd:,.2f} ({pnl_pct:+.1f}%)")
            shared_state.append_trade("sfp", {
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry_price"],
                "exit_price": price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
                "size_usdt": round(notional, 2),
                "leverage": self.leverage,
                "reason": reason,
                "entry_time": pos.get("entry_unix", 0),
            })

    def display_open_positions(self, tickers):
        """Show current paper positions with live P&L."""
        if not self.positions:
            return

        price_map = {}
        for t in tickers:
            try:
                price_map[t["symbol"]] = float(t["lastPrice"])
            except (KeyError, ValueError, TypeError):
                continue

        print(f"\n  {'─'*135}")
        print(f"  PAPER POSITIONS ({len(self.positions)} open)")
        print(f"  {'─'*135}")
        print(f"  {'Symbol':<14} {'Side':<6} {'Grade':<6} {'Entry':>12} {'Current':>12}"
              f"  {'P&L%':>8}  {'P&L$':>10}  {'SL':>12}  {'Trail':>6}  {'Entered':<22}  {'Held':>6}")
        print(f"  {'─'*135}")

        total_pnl = 0
        now = time.time()
        for symbol, pos in sorted(self.positions.items()):
            price = price_map.get(symbol, pos["entry_price"])
            side = pos["side"]
            entry = pos["entry_price"]

            if side == "long":
                pnl_pct = (price - entry) / entry * 100
                peak = pos.get("trail_high", entry)
                peak_pnl = (peak - entry) / entry * 100
            else:
                pnl_pct = (entry - price) / entry * 100
                peak = pos.get("trail_low", entry)
                peak_pnl = (entry - peak) / entry * 100

            pnl_usd = pnl_pct / 100 * self.amount
            total_pnl += pnl_usd
            trail_pct = self._get_trailing_pct(peak_pnl)
            trail_str = f"{trail_pct:.0f}%" if trail_pct > 0 else "—"
            sl_str = f"{pos['sl_price']:,.4g}" if pos.get("sl_price") else "—"
            held = self._format_elapsed(now - pos.get("entry_unix", now))

            print(f"  {symbol:<14} {side.upper():<6} {pos['grade']:<6}"
                  f" {entry:>12,.6g} {price:>12,.6g}"
                  f"  {pnl_pct:>+7.1f}%  ${pnl_usd:>+9,.2f}  {sl_str:>12}  {trail_str:>6}"
                  f"  {pos['entry_time']:<22}  {held:>6}")

        print(f"  {'─'*135}")
        print(f"  {'Total unrealized P&L:':>105}  ${total_pnl:>+9,.2f}")
        print()

    def display_periodic_summary(self, tickers):
        """Quick P&L recap shown after each scan cycle."""
        price_map = {}
        for t in tickers:
            try:
                price_map[t["symbol"]] = float(t["lastPrice"])
            except (KeyError, ValueError, TypeError):
                continue

        unrealized = 0
        for symbol, pos in self.positions.items():
            price = price_map.get(symbol, pos["entry_price"])
            if pos["side"] == "long":
                pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
            else:
                pnl_pct = (pos["entry_price"] - price) / pos["entry_price"] * 100
            unrealized += pnl_pct / 100 * self.amount

        realized = sum(t["pnl_usd"] for t in self.closed_trades)
        elapsed = time.time() - self.start_time
        mins = int(elapsed / 60)

        wins = sum(1 for t in self.closed_trades if t["pnl_usd"] >= 0)
        losses = sum(1 for t in self.closed_trades if t["pnl_usd"] < 0)

        print(f"\n  {'='*80}")
        print(f"  PAPER P&L UPDATE ({mins}m elapsed)")
        print(f"  {'─'*80}")
        print(f"    Open:      {len(self.positions)} position(s) | Unrealized: ${unrealized:+,.2f}")
        print(f"    Closed:    {len(self.closed_trades)} trade(s) | W:{wins} L:{losses} | Realized: ${realized:+,.2f}")
        print(f"    Combined:  ${realized + unrealized:+,.2f}")
        print(f"  {'='*80}")

    def display_summary(self):
        """Print final P&L summary when scanner stops."""
        elapsed = time.time() - self.start_time
        hours = elapsed / 3600
        mins = (elapsed % 3600) / 60

        print(f"\n{'='*90}")
        print(f"  PAPER TRADING SUMMARY")
        print(f"  Session: {int(hours)}h {int(mins)}m | "
              f"${self.amount} per trade @ {self.leverage}x leverage")
        print(f"  Stop loss: structural (beyond sweep + {SL_BUFFER_PCT}% buffer)")
        print(f"  Trailing tiers: 10%→8% | 30%→6% | 100%→3% | 300%→2%")
        print(f"{'='*90}")

        all_trades = list(self.closed_trades)

        open_count = len(self.positions)
        if open_count > 0:
            print(f"\n  {open_count} position(s) still open (not included in realized P&L)")

        if not all_trades:
            print(f"\n  No trades were closed during this session.")
            print(f"{'='*90}\n")
            return

        wins = [t for t in all_trades if t["pnl_usd"] >= 0]
        losses = [t for t in all_trades if t["pnl_usd"] < 0]
        total_pnl = sum(t["pnl_usd"] for t in all_trades)

        print(f"\n  CLOSED TRADES ({len(all_trades)}):")
        print(f"  {'─'*125}")
        print(f"  {'Symbol':<14} {'Side':<6} {'Grade':<6} {'Entry':>12} {'Exit':>12}"
              f"  {'P&L%':>8}  {'P&L$':>10}  {'Held':>6}  {'Entered':<22}  {'Reason'}")
        print(f"  {'─'*125}")

        for t in all_trades:
            held = self._format_elapsed(t.get("exit_unix", 0) - t.get("entry_unix", 0))
            print(f"  {t['symbol']:<14} {t['side'].upper():<6} {t['grade']:<6}"
                  f" {t['entry_price']:>12,.6g} {t['exit_price']:>12,.6g}"
                  f"  {t['pnl_pct']:>+7.1f}%  ${t['pnl_usd']:>+9,.2f}  {held:>6}  {t['entry_time']:<22}  {t['reason']}")

        print(f"  {'─'*125}")
        print(f"\n  RESULTS:")
        print(f"    Total trades:  {len(all_trades)}")
        print(f"    Wins:          {len(wins)} ({len(wins)/len(all_trades)*100:.0f}%)")
        print(f"    Losses:        {len(losses)} ({len(losses)/len(all_trades)*100:.0f}%)")
        if wins:
            print(f"    Avg win:       ${sum(t['pnl_usd'] for t in wins)/len(wins):+,.2f}")
        if losses:
            print(f"    Avg loss:      ${sum(t['pnl_usd'] for t in losses)/len(losses):+,.2f}")
        best = max(all_trades, key=lambda t: t["pnl_usd"])
        worst = min(all_trades, key=lambda t: t["pnl_usd"])
        print(f"    Best trade:    {best['symbol']} ${best['pnl_usd']:+,.2f} ({best['pnl_pct']:+.1f}%)")
        print(f"    Worst trade:   {worst['symbol']} ${worst['pnl_usd']:+,.2f} ({worst['pnl_pct']:+.1f}%)")
        print(f"\n    TOTAL P&L:     ${total_pnl:+,.2f}")
        print(f"{'='*90}\n")


# ──────────────────────────────────────────────
# Trading + Position Management
# ──────────────────────────────────────────────


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


def fetch_ticker_volumes(base_url, symbols):
    """Fetch 24h turnover for a list of symbols (single API call)."""
    url = f"{base_url}/v5/market/tickers?category=linear"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            tickers = data.get("result", {}).get("list", [])
            wanted = set(symbols)
            return {
                t["symbol"]: float(t.get("turnover24h", 0))
                for t in tickers if t.get("symbol") in wanted
            }
    except Exception:
        return {}


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

    # Fetch 24h volumes for all open position symbols
    pos_symbols = [p.get("symbol", "") for p in positions]
    volumes = fetch_ticker_volumes(base_url, pos_symbols)

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
        vol_24h = volumes.get(symbol, 0)
        vol_str = f"${vol_24h/1e6:,.1f}M" if vol_24h >= 1e6 else f"${vol_24h:,.0f}"

        print(f"\n  {symbol} {side} {lev:.0f}x  |  24h Vol: {vol_str}")
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
            if initial_sl_pct > 0:
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
    """Trade the best SFP signals. traded_symbols is dict[symbol -> last_trade_ts]."""
    min_grade = args.min_grade.upper()
    now_ts = time.time()

    def in_cooldown(sym):
        if sym not in traded_symbols:
            return False, 0
        elapsed = (now_ts - traded_symbols[sym]) / 3600
        return (elapsed < RE_ENTRY_COOLDOWN_HOURS), elapsed

    tradeable = []
    for r in results:
        if GRADE_ORDER.get(r["grade"], 99) > GRADE_ORDER.get(min_grade, 2):
            continue
        if r["sfp"]["candles_ago"] > 1:
            continue
        in_cd, _ = in_cooldown(r["symbol"])
        if in_cd:
            continue
        tradeable.append(r)

    if not tradeable:
        print(f"\n  No tradeable SFPs (grade {min_grade}+ and fresh, no cooldown).\n")
        return traded_symbols

    max_exposure = args.account_balance * args.max_exposure_mult
    current_exposure = 0
    if args.live:
        pos_data = auth_request(base_url, "GET", "/v5/position/list",
                                api_key, api_secret,
                                {"category": "linear", "settleCoin": "USDT"})
        for p in pos_data.get("result", {}).get("list", []):
            sz = float(p.get("size", "0") or "0")
            mp = float(p.get("markPrice", "0") or "0")
            if sz > 0:
                current_exposure += sz * mp

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
        tf = r.get("count_tf", "?")

        print(f"  >> {sfp_type} SFP [{grade}] on {symbol} ({tf}) @ ${price:,.6g}")

        remaining = max(0, max_exposure - current_exposure)
        trade_notional = min(args.amount, remaining)
        if trade_notional < 5:
            print(f"     SKIP: max exposure reached (${current_exposure:,.0f}/${max_exposure:,.0f})")
            break

        instrument = get_instrument_info(base_url, symbol)
        if not instrument:
            print(f"     SKIP: no instrument info")
            continue

        qty = calculate_qty(trade_notional, price, instrument)
        if not qty:
            print(f"     SKIP: qty too small")
            continue

        est_value = float(qty) * price
        print(f"     Order: {side.upper()} {qty} {symbol} (~${est_value:,.2f}) @ {args.leverage}x")

        if args.no_stops:
            sl_price = None
        elif side == "Buy":
            sl_price = round(price * (1 - args.initial_sl / 100), 6)
        else:
            sl_price = round(price * (1 + args.initial_sl / 100), 6)

        sl_msg = (f"NO SL (zero-hero)" if sl_price is None
                  else f"with SL at ${sl_price:,.6g} (-{args.initial_sl}%)")

        if not args.live:
            print(f"     [DRY-RUN] Would place order {sl_msg}")
            log_sfp_trade(symbol, side, qty, price, est_value, args.leverage,
                          grade, sfp_type, tf, "dry-run", "N/A", "dry-run")
            traded_symbols[symbol] = time.time()
            trades_this_cycle += 1
            current_exposure += est_value
        else:
            auth_request(base_url, "POST", "/v5/position/set-leverage",
                         api_key, api_secret, {
                             "category": "linear", "symbol": symbol,
                             "buyLeverage": str(args.leverage), "sellLeverage": str(args.leverage),
                         })
            time.sleep(0.3)

            print(f"     Placing order {sl_msg}...", end=" ")
            order_params = {
                "category": "linear", "symbol": symbol, "side": side,
                "orderType": "Market", "qty": qty, "positionIdx": 0,
                "orderLinkId": f"sfp_{symbol}_{int(time.time())}",
            }
            if sl_price is not None:
                order_params["stopLoss"] = str(sl_price)
            result = auth_request(base_url, "POST", "/v5/order/create",
                                  api_key, api_secret, order_params)
            ret = result.get("retCode", -1)
            oid = result.get("result", {}).get("orderId", "N/A")

            if ret == 0:
                print(f"FILLED (orderId: {oid})")
                log_sfp_trade(symbol, side, qty, price, est_value, args.leverage,
                              grade, sfp_type, tf, "filled", oid, "live")
                traded_symbols[symbol] = time.time()
                trades_this_cycle += 1
                current_exposure += est_value
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
                    traded_symbols[symbol] = time.time()
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
        description="Bybit SFP Scanner V2 — multi-TF Swing Failure Pattern detection"
    )
    parser.add_argument("--testnet", action="store_true", help="Use testnet")
    parser.add_argument("--min-volume", type=float, default=DEFAULT_MIN_VOLUME_M,
                        help=f"Min 24h turnover in millions USD (default: {DEFAULT_MIN_VOLUME_M})")
    # V2 detection params
    parser.add_argument("--pivot-tf", type=str, default=DEFAULT_PIVOT_TF,
                        help=f"Pivot timeframe (default: {DEFAULT_PIVOT_TF} = 1H)")
    parser.add_argument("--count-tf", type=str, default=DEFAULT_COUNT_TF,
                        help=f"Counting timeframe for breakout/reclaim + MSB (default: {DEFAULT_COUNT_TF} = 5m)")
    parser.add_argument("--pivot-left", type=int, default=DEFAULT_PIVOT_LEFT,
                        help=f"Left bars for pivot (default: {DEFAULT_PIVOT_LEFT})")
    parser.add_argument("--pivot-right", type=int, default=DEFAULT_PIVOT_RIGHT,
                        help=f"Right bars for pivot (default: {DEFAULT_PIVOT_RIGHT})")
    parser.add_argument("--pivot-source", type=str, default=DEFAULT_PIVOT_SOURCE,
                        choices=["wicks", "closes"],
                        help=f"Pivot source (default: {DEFAULT_PIVOT_SOURCE})")
    parser.add_argument("--min-bars", type=int, default=DEFAULT_MIN_BARS_BREAKOUT,
                        help=f"Min counting-TF bars closed outside (0 = wick-only, default: {DEFAULT_MIN_BARS_BREAKOUT})")
    parser.add_argument("--max-bars", type=int, default=DEFAULT_MAX_BARS_BREAKOUT,
                        help=f"Max counting-TF bars before reclaim (default: {DEFAULT_MAX_BARS_BREAKOUT})")
    parser.add_argument("--levels-to-scan", type=int, default=DEFAULT_LEVELS_TO_SCAN,
                        help=f"Recent pivot levels per side (default: {DEFAULT_LEVELS_TO_SCAN})")
    # MA filter
    parser.add_argument("--no-ma-filter", action="store_false", dest="ma_filter",
                        default=DEFAULT_MA_FILTER, help="Disable MA trend filter")
    parser.add_argument("--ma-tf", type=str, default=DEFAULT_MA_TF, help="MA timeframe")
    parser.add_argument("--ma-type", type=str, default=DEFAULT_MA_TYPE,
                        choices=["EMA", "SMA"], help="MA type")
    parser.add_argument("--ma-fast", type=int, default=DEFAULT_MA_FAST, help="Fast MA length")
    parser.add_argument("--ma-slow", type=int, default=DEFAULT_MA_SLOW, help="Slow MA length")
    # Structural filter
    parser.add_argument("--struct-filter", action="store_true",
                        default=DEFAULT_STRUCT_FILTER, help="Enable structural filter")
    parser.add_argument("--struct-count", type=int, default=DEFAULT_STRUCT_COUNT,
                        help=f"N pivots for structure direction (default: {DEFAULT_STRUCT_COUNT})")
    # MSB + Breaker Block confirmation
    parser.add_argument("--no-msb-filter", action="store_false", dest="msb_filter",
                        default=DEFAULT_MSB_FILTER, help="Disable MSB + breaker block confirmation")
    parser.add_argument("--msb-swing-left", type=int, default=DEFAULT_MSB_SWING_LEFT,
                        help=f"Left bars for M5 swing detection (default: {DEFAULT_MSB_SWING_LEFT})")
    parser.add_argument("--msb-swing-right", type=int, default=DEFAULT_MSB_SWING_RIGHT,
                        help=f"Right bars for M5 swing detection (default: {DEFAULT_MSB_SWING_RIGHT})")
    parser.add_argument("--msb-lookback", type=int, default=DEFAULT_MSB_LOOKBACK,
                        help=f"Max count-TF bars after SFP to find MSB (default: {DEFAULT_MSB_LOOKBACK})")
    parser.add_argument("--watch", type=int, default=15, help="Rescan interval in minutes (default: 15, 0 for one-shot)")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    # Trading flags
    parser.add_argument("--trade", action="store_true", help="Enable auto-trading (dry-run by default)")
    parser.add_argument("--live", action="store_true", help="Execute real trades (requires --trade)")
    parser.add_argument("--amount", type=float, default=DEFAULT_AMOUNT_USDT,
                        help=f"USDT per trade (default: {DEFAULT_AMOUNT_USDT})")
    parser.add_argument("--account-balance", type=float, default=DEFAULT_ACCOUNT_BALANCE,
                        help=f"Account balance for exposure limits (default: ${DEFAULT_ACCOUNT_BALANCE})")
    parser.add_argument("--max-exposure-mult", type=float, default=DEFAULT_MAX_EXPOSURE_MULT,
                        help=f"Max total exposure as multiple of account (default: {DEFAULT_MAX_EXPOSURE_MULT}x)")
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE,
                        help=f"Leverage (default: {DEFAULT_LEVERAGE}x)")
    parser.add_argument("--initial-sl", type=float, default=DEFAULT_INITIAL_SL_PCT,
                        help=f"Initial stop loss %% (default: {DEFAULT_INITIAL_SL_PCT})")
    parser.add_argument("--no-stops", action="store_true",
                        help="Zero-hero mode: no initial SL, trailing stops only")
    parser.add_argument("--min-grade", type=str, default=DEFAULT_MIN_TRADE_GRADE,
                        help=f"Min SFP grade to trade: A+, A, B, C (default: {DEFAULT_MIN_TRADE_GRADE})")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Skip interactive CONFIRM prompt (for dashboard/automation)")
    args = parser.parse_args()

    if args.no_stops:
        args.initial_sl = 0.0

    base_url = TESTNET_URL if args.testnet else MAINNET_URL
    env_label = "TESTNET" if args.testnet else "MAINNET"

    trading_mode = args.trade
    is_live = args.live and args.trade
    paper_mode = args.watch > 0 and not trading_mode
    if paper_mode:
        mode_str = "PAPER TRADING"
    elif is_live:
        mode_str = "LIVE"
    elif trading_mode:
        mode_str = "DRY-RUN"
    else:
        mode_str = "SCAN ONLY"

    api_key = api_secret = None
    pos_state = {}
    traded_symbols = {}      # symbol -> last_trade_unix_ts
    consumed_levels = {}     # symbol -> set of consumed level keys
    paper = None

    if paper_mode:
        paper = PaperTrader(args.amount, args.leverage, args.min_grade,
                            args.account_balance, args.max_exposure_mult)

    if trading_mode:
        api_key, api_secret = get_credentials()
        init_sfp_logs()
        pos_state = load_sfp_state()

        if is_live and not args.testnet and not args.no_confirm:
            print(f"\n  WARNING: LIVE SFP auto-trading on MAINNET.")
            print(f"  ${args.amount} per trade | {args.leverage}x | Min grade: {args.min_grade}")
            if args.no_stops:
                print(f"  ZERO-HERO MODE: no initial SL, trailing stops only")
            confirm = input("\n  Type CONFIRM to proceed: ").strip()
            if confirm.upper() != "CONFIRM":
                print("  Cancelled.")
                sys.exit(0)

    pivot_label = INTERVAL_LABELS.get(args.pivot_tf, args.pivot_tf)
    count_label = INTERVAL_LABELS.get(args.count_tf, args.count_tf)
    print(f"\n[{env_label}] Bybit SFP Scanner V2 [{mode_str}]")
    print(f"  Pivot TF:        {pivot_label} ({args.pivot_source}, L{args.pivot_left}/R{args.pivot_right})")
    print(f"  Count TF:        {count_label} (breakout {args.min_bars}-{args.max_bars} bars)")
    print(f"  Levels/side:     {args.levels_to_scan}")
    print(f"  Min volume:      ${args.min_volume}M")
    if args.ma_filter:
        ma_tf_label = INTERVAL_LABELS.get(args.ma_tf, args.ma_tf)
        print(f"  MA filter:       {args.ma_type} {args.ma_fast}/{args.ma_slow} on {ma_tf_label}")
    if args.struct_filter:
        print(f"  Struct filter:   N={args.struct_count}")
    if args.msb_filter:
        print(f"  MSB + BB filter: ON (swing L{args.msb_swing_left}/R{args.msb_swing_right}, lookback {args.msb_lookback} bars)")
    if paper_mode:
        max_exp = args.account_balance * args.max_exposure_mult
        print(f"  Paper trade:     ${args.amount} @ {args.leverage}x | Min grade: {args.min_grade}")
        print(f"  Account:         ${args.account_balance:,.0f} | Max exposure: ${max_exp:,.0f} ({args.max_exposure_mult}x)")
        print(f"  Stop loss:       Structural (beyond sweep + {SL_BUFFER_PCT}% buffer)")
        print(f"  Trailing tiers:  10%→8% | 30%→6% | 100%→3% | 300%→2%")
        print(f"  Re-entry after:  {RE_ENTRY_COOLDOWN_HOURS}h cooldown")
        print(f"  P&L summary shown after each scan. Ctrl+C for final summary")
    elif trading_mode:
        print(f"  Trade amount:    ${args.amount} @ {args.leverage}x")
        print(f"  Min grade:       {args.min_grade}")
        if args.no_stops:
            print(f"  Initial SL:      NONE — zero-hero (trailing stops only)")
        else:
            print(f"  Initial SL:      {args.initial_sl}%")
        print(f"  Re-entry after:  {RE_ENTRY_COOLDOWN_HOURS}h cooldown")
        print(f"  Trailing tiers:  10%→8% | 30%→6% | 100%→3% | 300%→2%")

    while True:
        results = run_sfp_scan(base_url, args, consumed_levels)
        display_results(results, env_label, args)

        # Paper trading: enter signals and update positions
        if paper and results:
            paper.enter_signals(results)

        if paper:
            tickers = fetch_linear_tickers(base_url)
            paper.update_prices(tickers)
            paper.display_open_positions(tickers)
            paper.display_periodic_summary(tickers)
            # Publish positions to dashboard
            price_map = {t["symbol"]: float(t["lastPrice"]) for t in tickers
                         if "lastPrice" in t}
            shared_state.write_positions("sfp", [
                {"symbol": sym, "side": p["side"], "entry_price": p["entry_price"],
                 "current_price": price_map.get(sym, p["entry_price"]),
                 "pnl_pct": round(((price_map.get(sym, p["entry_price"]) - p["entry_price"]) / p["entry_price"] * 100)
                                  if p["side"] == "long" else
                                  ((p["entry_price"] - price_map.get(sym, p["entry_price"])) / p["entry_price"] * 100), 2),
                 "size_usdt": paper.amount,
                 "leverage": paper.leverage,
                 "entry_time": p.get("entry_unix", 0),
                 "grade": p.get("grade", "")}
                for sym, p in paper.positions.items()
            ])

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
            if paper:
                paper.display_summary()
            break

        print(f"\n  Next scan + P&L update in {args.watch} min... (Ctrl+C to stop)\n")
        try:
            time.sleep(args.watch * 60)
        except KeyboardInterrupt:
            if paper:
                paper.display_summary()
            print("\n  Scanner stopped.")
            break


if __name__ == "__main__":
    main()
