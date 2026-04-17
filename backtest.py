#!/usr/bin/env python3
"""
Bybit Momentum Scanner Backtester

Replays historical data for specified coins and simulates:
  1. Momentum scanner running every 15 min (or custom interval)
  2. Auto trader entering when score hits threshold (default 60+)
  3. Position manager with adaptive trailing stops

Shows exactly when the system would have entered and exited,
and what profit/loss would have resulted.

Usage:
    python3 backtest.py                                  # default: RAVEUSDT, POWERUSDT, ARIAUSDT
    python3 backtest.py --symbols BTCUSDT ETHUSDT        # custom symbols
    python3 backtest.py --days 14                        # look back 14 days
    python3 backtest.py --min-score 50                   # trigger at 50+
    python3 backtest.py --amount 500 --leverage 10       # $500 at 10x

No API key required — uses public kline data only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
USER_AGENT = "bybit-skill/1.2.3"
MIN_CALL_INTERVAL = 0.12

# Momentum scanner parameters (must match momentum_scanner.py)
VOLUME_ALERT_MULTIPLIER = 3.0
VOLUME_EXTREME_MULTIPLIER = 8.0
OI_CHANGE_ALERT_PCT = 5.0
PRICE_ACCEL_THRESHOLD = 1.5

WEIGHTS = {
    "volume_anomaly": 0.30,
    "price_accel":    0.25,
    "oi_surge":       0.20,
    "funding_shift":  0.10,
    "streak":         0.15,
}

# Trailing stop tiers (must match position_manager.py)
TRAILING_TIERS = [
    (0,    0),
    (10,   8.0),
    (30,   6.0),
    (100,  3.0),
    (300,  2.0),
]

DEFAULT_INITIAL_SL_PCT = 8.0

# ──────────────────────────────────────────────
# Strategy presets for --compare mode
# ──────────────────────────────────────────────
#
# Each strategy defines:
#   - initial_sl_pct: float or None (None = no stop, only liquidation)
#   - trail_tiers:   list of (min_profit_pct, trail_pct) tuples, or []
#   - amount_mult:   size multiplier vs base amount (1.0 = same, 0.5 = half)
#   - leverage:      None to use CLI leverage, or override
#
STRATEGIES = {
    "current": {
        "name": "Current (8% SL + wider trail)",
        "initial_sl_pct": 8.0,
        "trail_tiers": [(0, 0), (10, 8.0), (30, 6.0), (100, 3.0), (300, 2.0)],
        "amount_mult": 1.0,
        "leverage_override": None,
    },
    "old_tight": {
        "name": "Old tight trail (5% SL, 5/3/2/1.5%)",
        "initial_sl_pct": 5.0,
        "trail_tiers": [(0, 0), (10, 5.0), (30, 3.0), (100, 2.0), (300, 1.5)],
        "amount_mult": 1.0,
        "leverage_override": None,
    },
    "no_stops_half": {
        "name": "No stops, half size (liquidation only)",
        "initial_sl_pct": None,
        "trail_tiers": [],
        "amount_mult": 0.5,
        "leverage_override": None,
    },
    "no_stops_low_lev": {
        "name": "3x leverage, no stops",
        "initial_sl_pct": None,
        "trail_tiers": [],
        "amount_mult": 1.0,
        "leverage_override": 3,
    },
    "wide_sl_trail": {
        "name": "15% SL + very wide trail (20/15/10/5%)",
        "initial_sl_pct": 15.0,
        "trail_tiers": [(0, 0), (30, 20.0), (100, 15.0), (300, 10.0)],
        "amount_mult": 1.0,
        "leverage_override": None,
    },
}

# ──────────────────────────────────────────────
# API client
# ──────────────────────────────────────────────

_last_call_ts = 0.0


def api_get(base_url, path, params=None):
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < MIN_CALL_INTERVAL:
        time.sleep(MIN_CALL_INTERVAL - elapsed)

    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{base_url}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "X-Referer": "bybit-skill",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _last_call_ts = time.time()
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"  Network error: {e}")
        return {"retCode": -1, "result": {}}


# ──────────────────────────────────────────────
# Data fetching — get full history
# ──────────────────────────────────────────────

def fetch_full_klines(base_url, symbol, interval, days):
    """
    Fetch kline history covering `days` of data.
    Bybit limits to 1000 candles per request, so we paginate.
    Returns oldest-first list of [time, open, high, low, close, volume, turnover].
    """
    all_klines = []
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 24 * 60 * 60 * 1000)

    # Calculate interval in ms
    interval_map = {
        "15": 15 * 60 * 1000,
        "60": 60 * 60 * 1000,
        "240": 4 * 60 * 60 * 1000,
    }
    interval_ms = interval_map.get(interval, 60 * 60 * 1000)
    candles_needed = (now_ms - start_ms) // interval_ms + 1

    print(f"    Fetching ~{candles_needed} candles ({days} days of {interval}min)...", end=" ")

    # Paginate backwards from now
    end_ms = now_ms
    retries = 0
    while end_ms > start_ms and retries < 50:
        data = api_get(base_url, "/v5/market/kline", {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": "1000",
            "end": str(end_ms),
        })
        klines = data.get("result", {}).get("list", [])
        if not klines:
            break

        all_klines.extend(klines)

        # Bybit returns newest first, so last item is oldest
        oldest_ts = int(klines[-1][0])
        if oldest_ts <= start_ms:
            break
        end_ms = oldest_ts - 1
        retries += 1

    # Deduplicate by timestamp and sort oldest first
    seen = set()
    unique = []
    for k in all_klines:
        ts = k[0]
        if ts not in seen:
            seen.add(ts)
            unique.append(k)
    unique.sort(key=lambda x: int(x[0]))

    # Filter to our date range
    unique = [k for k in unique if int(k[0]) >= start_ms]

    print(f"got {len(unique)} candles")
    return unique


def fetch_oi_history(base_url, symbol, days):
    """Fetch OI history (5min intervals). Limited data available."""
    all_oi = []
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 24 * 60 * 60 * 1000)

    end_ms = now_ms
    retries = 0
    while end_ms > start_ms and retries < 30:
        data = api_get(base_url, "/v5/market/open-interest", {
            "category": "linear",
            "symbol": symbol,
            "intervalTime": "1h",
            "limit": "200",
            "endTime": str(end_ms),
        })
        items = data.get("result", {}).get("list", [])
        if not items:
            break
        all_oi.extend(items)
        oldest_ts = int(items[-1].get("timestamp", 0))
        if oldest_ts <= start_ms:
            break
        end_ms = oldest_ts - 1
        retries += 1

    # Deduplicate and sort newest first (as API returns)
    seen = set()
    unique = []
    for item in all_oi:
        ts = item.get("timestamp", "")
        if ts not in seen:
            seen.add(ts)
            unique.append(item)
    unique.sort(key=lambda x: int(x.get("timestamp", 0)), reverse=True)
    return unique


# ──────────────────────────────────────────────
# Signal analyzers (same logic as momentum_scanner.py)
# ──────────────────────────────────────────────

def analyze_volume_anomaly(klines_window):
    if len(klines_window) < 24:
        return {"score": 0, "multiplier_1h": 0, "detail": "insufficient data"}

    turnovers = [float(k[6]) for k in klines_window]
    avg_24h = sum(turnovers) / len(turnovers) if turnovers else 1
    if avg_24h < 1:
        return {"score": 0, "multiplier_1h": 0, "detail": "near-zero volume"}

    last_1h = turnovers[-1]
    last_4h_avg = sum(turnovers[-4:]) / min(4, len(turnovers[-4:]))
    mult_1h = last_1h / avg_24h
    mult_4h = last_4h_avg / avg_24h
    peak_mult = max(mult_1h, mult_4h)

    if peak_mult <= 1:
        score = 0
    elif peak_mult <= VOLUME_ALERT_MULTIPLIER:
        score = (peak_mult - 1) / (VOLUME_ALERT_MULTIPLIER - 1) * 50
    elif peak_mult <= VOLUME_EXTREME_MULTIPLIER:
        score = 50 + (peak_mult - VOLUME_ALERT_MULTIPLIER) / (VOLUME_EXTREME_MULTIPLIER - VOLUME_ALERT_MULTIPLIER) * 30
    else:
        score = min(100, 80 + (peak_mult - VOLUME_EXTREME_MULTIPLIER) / 7 * 20)

    return {
        "score": round(score, 1),
        "multiplier_1h": round(mult_1h, 2),
        "multiplier_4h": round(mult_4h, 2),
        "detail": f"{mult_1h:.1f}x (1h) / {mult_4h:.1f}x (4h) vs avg",
    }


def analyze_price_acceleration(klines_window):
    if len(klines_window) < 12:
        return {"score": 0, "detail": "insufficient data"}

    closes = [float(k[4]) for k in klines_window]
    current = closes[-1]

    def pct_change(old, new):
        return ((new - old) / old * 100) if old > 0 else 0

    roc_1h = pct_change(closes[-2], current) if len(closes) >= 2 else 0
    roc_4h = pct_change(closes[-5], current) / 4 if len(closes) >= 5 else 0
    roc_12h = pct_change(closes[-13], current) / 12 if len(closes) >= 13 else 0

    if roc_12h > 0:
        accel_ratio = roc_1h / roc_12h if roc_12h != 0 else 0
    elif roc_4h > 0:
        accel_ratio = roc_1h / roc_4h if roc_4h != 0 else 0
    else:
        accel_ratio = 0

    abs_momentum = abs(roc_1h)

    if accel_ratio <= 0:
        accel_score = 0
    elif accel_ratio <= PRICE_ACCEL_THRESHOLD:
        accel_score = (accel_ratio / PRICE_ACCEL_THRESHOLD) * 40
    else:
        accel_score = min(100, 40 + (accel_ratio - PRICE_ACCEL_THRESHOLD) * 15)

    momentum_score = min(50, abs_momentum * 5)
    score = min(100, accel_score * 0.6 + momentum_score * 0.4)

    return {
        "score": round(score, 1),
        "roc_1h": round(roc_1h, 3),
        "accel_ratio": round(accel_ratio, 2),
        "detail": f"1h {roc_1h:+.2f}% | accel {accel_ratio:.1f}x",
    }


def analyze_oi_surge_at_time(oi_data, target_time_ms, window_hours=4):
    """Get OI change around a specific point in time."""
    window_ms = window_hours * 60 * 60 * 1000
    relevant = [
        o for o in oi_data
        if target_time_ms - window_ms <= int(o.get("timestamp", 0)) <= target_time_ms
    ]
    if len(relevant) < 2:
        return {"score": 0, "oi_change_pct": 0, "detail": "insufficient OI data"}

    relevant.sort(key=lambda x: int(x.get("timestamp", 0)))
    try:
        oldest_oi = float(relevant[0].get("openInterest", 0))
        newest_oi = float(relevant[-1].get("openInterest", 0))
    except (ValueError, TypeError):
        return {"score": 0, "oi_change_pct": 0, "detail": "parse error"}

    if oldest_oi <= 0:
        return {"score": 0, "oi_change_pct": 0, "detail": "no baseline"}

    oi_change_pct = ((newest_oi - oldest_oi) / oldest_oi) * 100

    if oi_change_pct <= 0:
        score = 0
    elif oi_change_pct <= OI_CHANGE_ALERT_PCT:
        score = (oi_change_pct / OI_CHANGE_ALERT_PCT) * 50
    elif oi_change_pct <= 15:
        score = 50 + (oi_change_pct - OI_CHANGE_ALERT_PCT) / 10 * 30
    else:
        score = min(100, 80 + (oi_change_pct - 15) / 15 * 20)

    return {
        "score": round(score, 1),
        "oi_change_pct": round(oi_change_pct, 2),
        "detail": f"OI {oi_change_pct:+.1f}% over ~{window_hours}h",
    }


def analyze_streak(klines_window):
    if len(klines_window) < 3:
        return {"score": 0, "detail": "insufficient data"}

    green_streak = 0
    for k in reversed(klines_window):
        if float(k[4]) > float(k[1]):
            green_streak += 1
        else:
            break

    vols = [float(k[5]) for k in klines_window]
    vol_rising = 0
    for i in range(len(vols) - 1, 0, -1):
        if vols[i] > vols[i - 1]:
            vol_rising += 1
        else:
            break

    green_score = min(60, green_streak * 10)
    vol_score = min(40, vol_rising * 8)
    score = min(100, green_score + vol_score)

    return {
        "score": round(score, 1),
        "green_streak": green_streak,
        "vol_rising": vol_rising,
        "detail": f"{green_streak} green, {vol_rising} rising-vol",
    }


def compute_momentum_score(signals):
    score = 0
    for key, weight in WEIGHTS.items():
        signal = signals.get(key, {})
        score += signal.get("score", 0) * weight
    return round(score, 1)


# ──────────────────────────────────────────────
# Trailing stop simulation
# ──────────────────────────────────────────────

def get_trail_tier(profit_pct, tiers):
    """Return the active trail tier for current profit."""
    if not tiers:
        return (0, 0)
    active = tiers[0]
    for min_p, trail in tiers:
        if profit_pct >= min_p:
            active = (min_p, trail)
    return active


def simulate_position(klines_from_entry, entry_price, leverage, initial_sl_pct,
                      amount_usdt, trail_tiers=None):
    """
    Simulate a long position through historical candles.

    If initial_sl_pct is None, no stop loss is set — position only exits via
    liquidation (approx -(90/leverage)% price move) or end of data.

    Returns (events, exit_price, profit_pct, pnl_usd, exit_type).
    """
    if trail_tiers is None:
        trail_tiers = TRAILING_TIERS

    # Set initial stop — None means no stop, use liquidation as floor
    if initial_sl_pct is None:
        # Liquidation ~ -(90/leverage)% to account for maintenance margin + fees
        liq_pct = 90.0 / leverage
        sl_price = entry_price * (1 - liq_pct / 100)
        stop_type = "LIQUIDATION"
    else:
        sl_price = entry_price * (1 - initial_sl_pct / 100)
        stop_type = "STOPPED OUT"

    # Also enforce liquidation even when a stop is set (can't go below liq)
    liq_price = entry_price * (1 - 90.0 / leverage / 100)

    trailing_active = False
    highest_price = entry_price
    current_tier_pct = 0

    events = []
    events.append({
        "candle": 0,
        "action": "ENTRY",
        "price": entry_price,
        "sl": sl_price,
        "trail": "OFF",
        "profit_pct": 0,
    })

    for i, k in enumerate(klines_from_entry):
        high = float(k[2])
        low = float(k[3])
        close = float(k[4])
        ts = int(k[0])
        candle_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

        if high > highest_price:
            highest_price = high

        # Check liquidation first (applies regardless of stop)
        if low <= liq_price:
            exit_price = liq_price
            profit_pct = (exit_price - entry_price) / entry_price * 100
            pnl_usd = amount_usdt * leverage * profit_pct / 100
            events.append({
                "candle": i,
                "time": candle_time.strftime("%Y-%m-%d %H:%M"),
                "action": "LIQUIDATED",
                "price": exit_price,
                "profit_pct": round(profit_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
            })
            return events, exit_price, profit_pct, pnl_usd, "liquidated"

        # Check if stop loss was hit (only if we have a real stop)
        if initial_sl_pct is not None and low <= sl_price:
            exit_price = sl_price
            profit_pct = (exit_price - entry_price) / entry_price * 100
            pnl_usd = amount_usdt * leverage * profit_pct / 100
            events.append({
                "candle": i,
                "time": candle_time.strftime("%Y-%m-%d %H:%M"),
                "action": stop_type,
                "price": exit_price,
                "profit_pct": round(profit_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
            })
            return events, exit_price, profit_pct, pnl_usd, "stopped_out"

        # If no stops configured, skip trailing logic entirely
        if initial_sl_pct is None:
            continue

        profit_pct = (close - entry_price) / entry_price * 100
        _, tier_trail = get_trail_tier(profit_pct, trail_tiers)

        if tier_trail > 0 and (tier_trail != current_tier_pct):
            if current_tier_pct == 0 or tier_trail < current_tier_pct:
                current_tier_pct = tier_trail
                trailing_active = True
                trail_distance = highest_price * tier_trail / 100
                new_sl = highest_price - trail_distance
                if new_sl > sl_price:
                    sl_price = new_sl
                    events.append({
                        "candle": i,
                        "time": candle_time.strftime("%Y-%m-%d %H:%M"),
                        "action": f"TRAIL {tier_trail}%",
                        "price": close,
                        "sl": round(sl_price, 6),
                        "profit_pct": round(profit_pct, 2),
                    })

        if trailing_active and current_tier_pct > 0:
            trail_distance = highest_price * current_tier_pct / 100
            new_sl = highest_price - trail_distance
            if new_sl > sl_price:
                sl_price = new_sl

    # Position still open at end of data
    final_close = float(klines_from_entry[-1][4])
    profit_pct = (final_close - entry_price) / entry_price * 100
    pnl_usd = amount_usdt * leverage * profit_pct / 100
    events.append({
        "candle": len(klines_from_entry) - 1,
        "action": "STILL OPEN",
        "price": final_close,
        "profit_pct": round(profit_pct, 2),
        "pnl_usd": round(pnl_usd, 2),
    })
    return events, final_close, profit_pct, pnl_usd, "still_open"


# ──────────────────────────────────────────────
# Main backtest
# ──────────────────────────────────────────────

def run_backtest(base_url, symbol, days, scan_interval_candles, min_score,
                 amount_usdt, leverage, initial_sl_pct,
                 trail_tiers=None, preloaded_data=None):
    """
    Run full backtest for a single symbol.
    scan_interval_candles: how many 1h candles between scans (1 = every hour)
    preloaded_data: tuple of (klines_1h, oi_data) to avoid re-fetching
    """
    # Use preloaded data if given (for --compare mode to avoid re-fetching)
    if preloaded_data:
        klines_1h, oi_data = preloaded_data
    else:
        print(f"\n{'='*80}")
        print(f"  BACKTESTING: {symbol}")
        print(f"{'='*80}")
        print(f"\n  Fetching historical data ({days} days)...")
        klines_1h = fetch_full_klines(base_url, symbol, "60", days)
        if len(klines_1h) < 48:
            print(f"  Not enough data for {symbol} ({len(klines_1h)} candles). Skipping.")
            return None

        print(f"    Fetching OI history...", end=" ")
        oi_data = fetch_oi_history(base_url, symbol, days)
        print(f"got {len(oi_data)} data points")

    # Price range
    all_closes = [float(k[4]) for k in klines_1h]
    min_price = min(all_closes)
    max_price = max(all_closes)
    first_price = all_closes[0]
    last_price = all_closes[-1]
    total_move = (max_price - min_price) / min_price * 100

    first_time = datetime.fromtimestamp(int(klines_1h[0][0]) / 1000, tz=timezone.utc)
    last_time = datetime.fromtimestamp(int(klines_1h[-1][0]) / 1000, tz=timezone.utc)

    print(f"\n  Data range: {first_time.strftime('%Y-%m-%d %H:%M')} → {last_time.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Price range: ${min_price:,.6g} → ${max_price:,.6g} (max move: {total_move:+.1f}%)")
    print(f"  Start: ${first_price:,.6g} | End: ${last_price:,.6g} | Overall: {(last_price-first_price)/first_price*100:+.1f}%")

    # Scan through history
    print(f"\n  Scanning for momentum signals (every {scan_interval_candles}h)...")

    trades = []
    active_position = None
    signal_log = []

    # Need at least 48 candles of lookback for the scanner
    lookback = 48
    scan_points = range(lookback, len(klines_1h), scan_interval_candles)

    for idx in scan_points:
        candle = klines_1h[idx]
        scan_time_ms = int(candle[0])
        scan_time = datetime.fromtimestamp(scan_time_ms / 1000, tz=timezone.utc)
        current_price = float(candle[4])

        # Use only data available at this point in time (no future data)
        window = klines_1h[max(0, idx - 47):idx + 1]

        # Calculate signals
        signals = {
            "volume_anomaly": analyze_volume_anomaly(window),
            "price_accel": analyze_price_acceleration(window),
            "oi_surge": analyze_oi_surge_at_time(oi_data, scan_time_ms),
            "funding_shift": {"score": 0, "detail": "N/A (backtest)"},  # funding not in klines
            "streak": analyze_streak(window),
        }

        score = compute_momentum_score(signals)

        # Record significant signals
        if score >= min_score * 0.7:  # log signals approaching threshold too
            signal_log.append({
                "time": scan_time.strftime("%Y-%m-%d %H:%M"),
                "price": current_price,
                "score": score,
                "vol": signals["volume_anomaly"].get("multiplier_1h", 0),
                "accel": signals["price_accel"].get("roc_1h", 0),
                "oi": signals["oi_surge"].get("oi_change_pct", 0),
                "streak": signals["streak"].get("detail", ""),
                "triggered": score >= min_score and active_position is None,
            })

        # Check if we should enter
        if score >= min_score and active_position is None:
            entry_price = current_price
            remaining_klines = klines_1h[idx + 1:]

            if len(remaining_klines) < 2:
                continue

            events, exit_price, profit_pct, pnl_usd, exit_type = simulate_position(
                remaining_klines, entry_price, leverage, initial_sl_pct, amount_usdt,
                trail_tiers=trail_tiers,
            )

            trade = {
                "entry_time": scan_time.strftime("%Y-%m-%d %H:%M"),
                "entry_price": entry_price,
                "entry_score": score,
                "exit_price": exit_price,
                "profit_pct": round(profit_pct, 2),
                "leveraged_pct": round(profit_pct * leverage, 2),
                "pnl_usd": round(pnl_usd, 2),
                "exit_type": exit_type,
                "events": events,
                "signals_at_entry": {
                    "volume": signals["volume_anomaly"]["detail"],
                    "price_accel": signals["price_accel"]["detail"],
                    "oi": signals["oi_surge"]["detail"],
                    "streak": signals["streak"]["detail"],
                },
            }
            trades.append(trade)

            # Find when position closed to allow re-entry
            if exit_type == "stopped_out":
                exit_candle_idx = idx + 1 + events[-1]["candle"]
                # Skip ahead past exit + cooldown
                active_position = exit_candle_idx + 2
            else:
                active_position = len(klines_1h)  # still open, no re-entry

        # Allow re-entry after position closed
        if active_position is not None and idx > active_position:
            active_position = None

    return {
        "symbol": symbol,
        "days": days,
        "klines_count": len(klines_1h),
        "total_move_pct": round(total_move, 1),
        "start_price": first_price,
        "end_price": last_price,
        "peak_price": max_price,
        "trades": trades,
        "signal_log": signal_log,
    }


def display_backtest(result, amount_usdt, leverage, min_score):
    """Display backtest results for a symbol."""
    if not result:
        return

    symbol = result["symbol"]
    trades = result["trades"]

    print(f"\n{'─'*80}")
    print(f"  RESULTS: {symbol}")
    print(f"{'─'*80}")

    if not trades:
        print(f"\n  No trades triggered (score never hit {min_score}+)")

        # Show signal log to see how close it got
        signals = result.get("signal_log", [])
        if signals:
            top = sorted(signals, key=lambda s: s["score"], reverse=True)[:5]
            print(f"\n  Closest signals:")
            for s in top:
                tag = " << WOULD TRIGGER" if s["score"] >= min_score else ""
                print(f"    {s['time']} | Score: {s['score']:5.1f} | "
                      f"${s['price']:,.6g} | Vol: {s['vol']:.1f}x{tag}")
        return

    # Trade details
    total_pnl = 0
    for i, t in enumerate(trades, 1):
        print(f"\n  Trade #{i}")
        print(f"    Entry:    {t['entry_time']} @ ${t['entry_price']:,.6g} (score: {t['entry_score']})")
        print(f"    Signals:  Vol: {t['signals_at_entry']['volume']}")
        print(f"              Accel: {t['signals_at_entry']['price_accel']}")
        print(f"              OI: {t['signals_at_entry']['oi']}")
        print(f"              Streak: {t['signals_at_entry']['streak']}")

        # Show key events
        events = t["events"]
        for e in events:
            if e["action"] == "ENTRY":
                continue
            elif e["action"].startswith("TRAIL"):
                print(f"    Trail:    {e.get('time', '')} | {e['action']} | "
                      f"SL → ${e.get('sl', 0):,.6g} | +{e.get('profit_pct', 0):.1f}%")
            elif e["action"] in ("STOPPED OUT", "STILL OPEN"):
                exit_label = "Exit" if e["action"] == "STOPPED OUT" else "Status"
                print(f"    {exit_label}:    {e.get('time', 'end of data')} @ ${e['price']:,.6g} | "
                      f"{e['action']}")

        pnl_color = "+" if t["pnl_usd"] >= 0 else ""
        print(f"    Result:   {t['profit_pct']:+.2f}% price ({t['leveraged_pct']:+.1f}% with {leverage}x)")
        print(f"    P&L:      {pnl_color}${t['pnl_usd']:,.2f} (on ${amount_usdt} position)")
        total_pnl += t["pnl_usd"]

    # Summary
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses = sum(1 for t in trades if t["pnl_usd"] <= 0)
    best = max(trades, key=lambda t: t["pnl_usd"])
    worst = min(trades, key=lambda t: t["pnl_usd"])

    print(f"\n  {'─'*40}")
    print(f"  SUMMARY: {symbol}")
    print(f"  {'─'*40}")
    print(f"  Total trades:   {len(trades)}")
    print(f"  Wins / Losses:  {wins}W / {losses}L ({wins/(wins+losses)*100:.0f}% win rate)" if trades else "")
    print(f"  Best trade:     {best['profit_pct']:+.1f}% (${best['pnl_usd']:+,.2f})")
    print(f"  Worst trade:    {worst['profit_pct']:+.1f}% (${worst['pnl_usd']:+,.2f})")
    print(f"  Total P&L:      ${total_pnl:+,.2f}")
    print(f"  ROI:            {total_pnl / amount_usdt * 100:+.1f}% on ${amount_usdt} capital")

    return total_pnl


def run_strategy_comparison(base_url, symbols, days, scan_interval, min_score,
                             base_amount, base_leverage):
    """
    Run the same set of symbols against all STRATEGIES and compare results.
    Fetches data once per symbol, then simulates each strategy.
    """
    print(f"\n{'='*90}")
    print(f"  STRATEGY COMPARISON — {len(symbols)} symbol(s), {days} days, score {min_score}+")
    print(f"{'='*90}")
    print(f"  Base position: ${base_amount} @ {base_leverage}x")
    print(f"\n  Strategies being compared:")
    for key, strat in STRATEGIES.items():
        lev = strat["leverage_override"] or base_leverage
        amt = base_amount * strat["amount_mult"]
        sl_str = f"{strat['initial_sl_pct']}% SL" if strat["initial_sl_pct"] else "NO SL (liq only)"
        print(f"    [{key}] {strat['name']} — ${amt:.0f} @ {lev}x, {sl_str}")
    print()

    # results[strategy_key][symbol] = total_pnl
    strategy_results = {key: {"pnl": 0.0, "trades": 0, "wins": 0,
                               "liquidations": 0, "still_open": 0, "per_symbol": {}}
                        for key in STRATEGIES}

    for symbol in symbols:
        print(f"\n{'─'*90}")
        print(f"  {symbol}")
        print(f"{'─'*90}")

        # Fetch data once
        print(f"  Fetching data...", end=" ")
        klines_1h = fetch_full_klines(base_url, symbol, "60", days)
        if len(klines_1h) < 48:
            print(f"insufficient ({len(klines_1h)} candles). Skipping.")
            continue
        print(f"got {len(klines_1h)} candles.", end=" ")
        oi_data = fetch_oi_history(base_url, symbol, days)
        print(f"OI: {len(oi_data)} points")

        preloaded = (klines_1h, oi_data)

        # Run each strategy using the same data
        for key, strat in STRATEGIES.items():
            leverage = strat["leverage_override"] or base_leverage
            amount = base_amount * strat["amount_mult"]
            sl = strat["initial_sl_pct"]
            tiers = strat["trail_tiers"] if strat["trail_tiers"] else [(0, 0)]

            result = run_backtest(
                base_url, symbol, days, scan_interval, min_score,
                amount, leverage, sl if sl is not None else 999,  # dummy sl for signal loop
                trail_tiers=tiers, preloaded_data=preloaded,
            )
            # If initial_sl_pct was None, we need to re-run the trade simulation
            # bypassing the normal stop. Do this by re-simulating trades.
            if sl is None and result and result["trades"]:
                for trade in result["trades"]:
                    # Find entry in klines
                    entry_time_str = trade["entry_time"]
                    entry_idx = None
                    for idx, k in enumerate(klines_1h):
                        k_time = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
                        if k_time.strftime("%Y-%m-%d %H:%M") == entry_time_str:
                            entry_idx = idx
                            break
                    if entry_idx is None:
                        continue
                    remaining = klines_1h[entry_idx + 1:]
                    if len(remaining) < 2:
                        continue
                    events, exit_price, profit_pct, pnl_usd, exit_type = simulate_position(
                        remaining, trade["entry_price"], leverage,
                        None, amount, trail_tiers=[]
                    )
                    trade["exit_price"] = exit_price
                    trade["profit_pct"] = round(profit_pct, 2)
                    trade["leveraged_pct"] = round(profit_pct * leverage, 2)
                    trade["pnl_usd"] = round(pnl_usd, 2)
                    trade["exit_type"] = exit_type
                    trade["events"] = events

            if not result:
                continue

            # Aggregate
            sym_pnl = sum(t["pnl_usd"] for t in result["trades"])
            strategy_results[key]["pnl"] += sym_pnl
            strategy_results[key]["trades"] += len(result["trades"])
            strategy_results[key]["wins"] += sum(1 for t in result["trades"] if t["pnl_usd"] > 0)
            strategy_results[key]["liquidations"] += sum(1 for t in result["trades"] if t["exit_type"] == "liquidated")
            strategy_results[key]["still_open"] += sum(1 for t in result["trades"] if t["exit_type"] == "still_open")
            strategy_results[key]["per_symbol"][symbol] = {
                "pnl": sym_pnl,
                "trades": len(result["trades"]),
                "best": max((t["pnl_usd"] for t in result["trades"]), default=0),
                "worst": min((t["pnl_usd"] for t in result["trades"]), default=0),
            }

        # Print per-symbol row
        print(f"\n  Per-strategy P&L for {symbol}:")
        for key, strat in STRATEGIES.items():
            sym_data = strategy_results[key]["per_symbol"].get(symbol)
            if sym_data and sym_data["trades"] > 0:
                print(f"    [{key:20}] {sym_data['trades']} trades | "
                      f"P&L: ${sym_data['pnl']:+,.2f} | "
                      f"Best: ${sym_data['best']:+,.2f} | Worst: ${sym_data['worst']:+,.2f}")
            else:
                print(f"    [{key:20}] No trades triggered")

    # Final comparison table
    print(f"\n\n{'='*90}")
    print(f"  STRATEGY COMPARISON — FINAL RESULTS")
    print(f"{'='*90}")
    print(f"  {'Strategy':<35} {'Trades':>7} {'Wins':>6} {'Liq':>5} {'Open':>5} {'Total P&L':>14}")
    print(f"  {'-'*90}")

    ranked = sorted(strategy_results.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for key, data in ranked:
        name = STRATEGIES[key]["name"]
        if len(name) > 34:
            name = name[:31] + "..."
        wr = f"{data['wins']}/{data['trades']}" if data["trades"] > 0 else "0/0"
        print(f"  {name:<35} {data['trades']:>7} {wr:>6} "
              f"{data['liquidations']:>5} {data['still_open']:>5} "
              f"${data['pnl']:>+12,.2f}")

    print(f"  {'-'*90}")
    winner = ranked[0]
    print(f"\n  WINNER: [{winner[0]}] {STRATEGIES[winner[0]]['name']}")
    print(f"           Total P&L: ${winner[1]['pnl']:+,.2f}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Backtest the momentum scanner + position manager on historical data"
    )
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols to backtest (e.g. --symbols BTCUSDT ETHUSDT)")
    parser.add_argument("--days", type=int, default=10,
                        help="Days of history to analyze (default: 10)")
    parser.add_argument("--min-score", type=float, default=60,
                        help="Momentum score trigger threshold (default: 60)")
    parser.add_argument("--amount", type=float, default=500,
                        help="USDT per trade (default: 500)")
    parser.add_argument("--leverage", type=int, default=10,
                        help="Leverage (default: 10x)")
    parser.add_argument("--initial-sl", type=float, default=DEFAULT_INITIAL_SL_PCT,
                        help="Initial stop loss %% (default: 5)")
    parser.add_argument("--scan-interval", type=int, default=1,
                        help="Scan interval in hours (default: 1 = every candle)")
    parser.add_argument("--testnet", action="store_true", help="Use testnet")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    parser.add_argument("--compare", action="store_true",
                        help="Compare multiple strategies side-by-side on the same symbols")
    args = parser.parse_args()

    base_url = TESTNET_URL if args.testnet else MAINNET_URL
    env_label = "TESTNET" if args.testnet else "MAINNET"

    all_results = []
    grand_total_pnl = 0
    symbols_tested = 0

    def print_settings():
        print(f"\n{'='*80}")
        print(f"  MOMENTUM SCANNER BACKTEST [{env_label}]")
        print(f"{'='*80}")
        print(f"  Period:         last {args.days} days")
        print(f"  Trigger:        score {args.min_score}+")
        print(f"  Position:       ${args.amount} @ {args.leverage}x leverage")
        print(f"  Initial SL:     {args.initial_sl}%")
        print(f"  Trailing tiers: 10%→8% trail, 30%→6%, 100%→3%, 300%→2%")
        print(f"  Scan interval:  every {args.scan_interval}h")

    def run_for_symbols(symbols):
        nonlocal grand_total_pnl, symbols_tested
        for symbol in symbols:
            result = run_backtest(
                base_url, symbol, args.days, args.scan_interval,
                args.min_score, args.amount, args.leverage, args.initial_sl,
            )
            if result:
                all_results.append(result)
                pnl = display_backtest(result, args.amount, args.leverage, args.min_score)
                if pnl:
                    grand_total_pnl += pnl
                symbols_tested += 1

    def print_grand_summary():
        if not all_results:
            return
        print(f"\n{'='*80}")
        print(f"  GRAND TOTAL ACROSS ALL SYMBOLS")
        print(f"{'='*80}")
        total_trades = sum(len(r["trades"]) for r in all_results)
        total_wins = sum(1 for r in all_results for t in r["trades"] if t["pnl_usd"] > 0)
        print(f"  Symbols tested: {symbols_tested}")
        print(f"  Total trades:   {total_trades}")
        if total_trades > 0:
            print(f"  Win rate:       {total_wins}/{total_trades} ({total_wins/total_trades*100:.0f}%)")
        print(f"  Combined P&L:   ${grand_total_pnl:+,.2f}")
        if symbols_tested > 0:
            print(f"  Combined ROI:   {grand_total_pnl / (args.amount * symbols_tested) * 100:+.1f}% "
                  f"on ${args.amount * symbols_tested:,.0f} total capital")
        print()

    # Compare mode: run all strategies side-by-side
    if args.compare:
        if not args.symbols:
            print("\n  --compare requires --symbols. Example:")
            print("    python3 backtest.py --compare --symbols RAVE INX TRU BLESS ENJ\n")
            return
        # Auto-append USDT if missing
        symbols = []
        for s in args.symbols:
            s = s.upper()
            if not s.endswith("USDT") and not s.endswith("USD"):
                s = s + "USDT"
            symbols.append(s)
        run_strategy_comparison(
            base_url, symbols, args.days, args.scan_interval,
            args.min_score, args.amount, args.leverage,
        )
        return

    # If symbols passed via CLI, run them and done
    if args.symbols:
        print_settings()
        print(f"  Symbols:        {', '.join(args.symbols)}")
        run_for_symbols(args.symbols)
        print_grand_summary()
        if args.save:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"backtest_{ts}.json"
            with open(filename, "w") as f:
                json.dump(all_results, f, indent=2, default=str)
            print(f"  Full results saved to {filename}")
        return

    # Interactive mode — ask the user which coins to backtest
    print_settings()
    print(f"\n  Enter coin names to backtest (e.g. RAVE, BTC, ETHUSDT)")
    print(f"  You can enter multiple separated by spaces/commas")
    print(f"  Type 'done' or 'q' to finish and see the grand summary\n")

    while True:
        try:
            user_input = input("  Coin(s) to backtest: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input or user_input.lower() in ("done", "q", "quit", "exit"):
            break

        # Parse input: split by spaces, commas, or both
        raw = user_input.replace(",", " ").split()
        symbols = []
        for s in raw:
            s = s.strip().upper()
            if not s:
                continue
            # Auto-append USDT if not already there
            if not s.endswith("USDT") and not s.endswith("USD"):
                s = s + "USDT"
            symbols.append(s)

        if symbols:
            run_for_symbols(symbols)
            print_grand_summary()
            print(f"  Enter more coins or type 'done' to finish\n")

    print_grand_summary()

    if args.save and all_results:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"backtest_{ts}.json"
        with open(filename, "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"  Full results saved to {filename}")


if __name__ == "__main__":
    main()
