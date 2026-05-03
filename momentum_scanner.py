#!/usr/bin/env python3
"""
Bybit Momentum Scanner — Detect coins before they spike.

Scans all linear perpetual contracts for early momentum signals:
  1. Volume anomaly    — recent volume vs 24h baseline (3x+ = alert)
  2. Price acceleration — rate-of-change speeding up across timeframes
  3. OI surge          — open interest rising = new money entering
  4. Funding rate shift — rapid repositioning by traders
  5. Streak detection   — consecutive green candles with rising volume

Each signal is scored and weighted into a composite Momentum Score (0-100).
Coins are ranked and displayed with actionable alerts.

Usage:
    python3 momentum_scanner.py                  # one-shot scan
    python3 momentum_scanner.py --watch 5        # rescan every 5 minutes
    python3 momentum_scanner.py --min-score 60   # only show score >= 60
    python3 momentum_scanner.py --top 10         # show top 10 only
    python3 momentum_scanner.py --testnet        # use testnet

No API key required — all endpoints are public.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
USER_AGENT = "bybit-skill/1.2.3"
MIN_CALL_INTERVAL_GET = 0.12   # 120ms between GET requests
MIN_CALL_INTERVAL_POST = 0.32  # 320ms between POST requests

# Scoring weights (sum = 1.0)
WEIGHTS = {
    "volume_anomaly": 0.10,
    "price_accel":    0.10,
    "oi_surge":       0.10,
    "squeeze_setup":  0.10,   # negative funding + rising OI + rising price (now)
    "pre_squeeze":    0.20,   # MYX-style trap pattern (sustained setup)
    "accumulation":   0.30,   # early accumulation — volume ramp, OI from dead, quiet buildup
    "streak":         0.10,
}

# Distribution risk caps the penalty at -30 points
DISTRIBUTION_PENALTY_CAP = 30.0

# Thresholds
VOLUME_ALERT_MULTIPLIER = 3.0     # 3x average volume = notable
VOLUME_EXTREME_MULTIPLIER = 8.0   # 8x+ = extreme
OI_CHANGE_ALERT_PCT = 5.0         # 5% OI increase in recent window
PRICE_ACCEL_THRESHOLD = 1.5       # acceleration ratio threshold
MIN_TURNOVER_24H = 100_000        # lowered to catch quiet coins waking up
MIN_PRICE_CHANGE_PCT = 1.0        # skip coins with < 1% move (noise filter)

# ──────────────────────────────────────────────
# Rate-limited API client
# ──────────────────────────────────────────────

_last_call_ts = 0.0
_call_count = 0
_rate_limit_hits = 0


def api_get(base_url: str, path: str, params: dict | None = None) -> dict:
    """Rate-limited GET request to Bybit public API."""
    global _last_call_ts, _call_count, _rate_limit_hits

    # Backoff if we've hit rate limits
    if _rate_limit_hits >= 3:
        time.sleep(10)
        _rate_limit_hits = 0

    elapsed = time.time() - _last_call_ts
    if elapsed < MIN_CALL_INTERVAL_GET:
        time.sleep(MIN_CALL_INTERVAL_GET - elapsed)

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
                wait = 0.5 + (_rate_limit_hits * 0.5)
                time.sleep(wait)
                return api_get(base_url, path, params)  # retry
            return data
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"retCode": -1, "result": {}}


# ──────────────────────────────────────────────
# Data fetchers
# ──────────────────────────────────────────────

def fetch_all_linear_tickers(base_url: str) -> list[dict]:
    """Fetch all linear perpetual tickers."""
    data = api_get(base_url, "/v5/market/tickers", {"category": "linear"})
    return data.get("result", {}).get("list", [])


def fetch_klines(base_url: str, symbol: str, interval: str = "60",
                 limit: int = 48) -> list[list]:
    """Fetch klines (newest first from API, returned oldest-first).
    Each kline: [startTime, open, high, low, close, volume, turnover]
    """
    data = api_get(base_url, "/v5/market/kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
    })
    klines = data.get("result", {}).get("list", [])
    return list(reversed(klines))  # oldest first


def fetch_open_interest(base_url: str, symbol: str) -> list[dict]:
    """Fetch recent OI data (5min intervals, last 4 hours)."""
    data = api_get(base_url, "/v5/market/open-interest", {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": "5min",
        "limit": "48",  # 48 x 5min = 4 hours
    })
    return data.get("result", {}).get("list", [])


def fetch_funding_history(base_url: str, symbol: str) -> list[dict]:
    """Fetch recent funding rate history."""
    data = api_get(base_url, "/v5/market/funding/history", {
        "category": "linear",
        "symbol": symbol,
        "limit": "10",
    })
    return data.get("result", {}).get("list", [])


def fetch_daily_klines(base_url: str, symbol: str, limit: int = 14) -> list[list]:
    """Fetch daily klines for multi-day trend analysis (oldest first)."""
    data = api_get(base_url, "/v5/market/kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": "D",
        "limit": str(limit),
    })
    klines = data.get("result", {}).get("list", [])
    return list(reversed(klines))


def fetch_long_short_ratio(base_url: str, symbol: str) -> list[dict]:
    """
    Fetch long/short account ratio (Bybit /v5/market/account-ratio).
    Ratio > 1 = more longs than shorts; < 1 = more shorts.
    A ratio dropping hard while price rises signals short capitulation (bullish squeeze fuel).
    """
    data = api_get(base_url, "/v5/market/account-ratio", {
        "category": "linear",
        "symbol": symbol,
        "period": "5min",
        "limit": "12",  # last hour of data
    })
    return data.get("result", {}).get("list", [])


# ──────────────────────────────────────────────
# Signal analyzers
# ──────────────────────────────────────────────

def analyze_volume_anomaly(klines: list[list]) -> dict:
    """
    Compare recent volume windows to the 24h baseline.
    Returns volume multipliers and a 0-100 score.

    Uses turnover (quote volume in USDT) for fair comparison.
    """
    if len(klines) < 24:
        return {"score": 0, "multiplier_1h": 0, "multiplier_4h": 0, "detail": "insufficient data"}

    turnovers = [float(k[6]) for k in klines]

    # Baseline: average hourly turnover over 24h (or available data)
    avg_24h = sum(turnovers) / len(turnovers) if turnovers else 1

    if avg_24h < 1:
        return {"score": 0, "multiplier_1h": 0, "multiplier_4h": 0, "detail": "near-zero volume"}

    # Recent windows
    last_1h = turnovers[-1] if len(turnovers) >= 1 else 0
    last_4h_avg = sum(turnovers[-4:]) / min(4, len(turnovers[-4:])) if len(turnovers) >= 4 else last_1h

    mult_1h = last_1h / avg_24h
    mult_4h = last_4h_avg / avg_24h

    # Score: 0 at 1x, 50 at 3x, 80 at 8x, 100 at 15x+
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
        "avg_24h_turnover": round(avg_24h, 0),
        "last_1h_turnover": round(last_1h, 0),
        "detail": f"{mult_1h:.1f}x (1h) / {mult_4h:.1f}x (4h) vs avg",
    }


def analyze_price_acceleration(klines: list[list]) -> dict:
    """
    Detect accelerating price movement across timeframes.
    Compares rate of change over 1h, 4h, and 12h windows.
    Acceleration = shorter-window RoC growing faster than longer-window RoC.
    """
    if len(klines) < 12:
        return {"score": 0, "roc_1h": 0, "roc_4h": 0, "roc_12h": 0, "detail": "insufficient data"}

    closes = [float(k[4]) for k in klines]
    current = closes[-1]

    def pct_change(old, new):
        return ((new - old) / old * 100) if old > 0 else 0

    roc_1h = pct_change(closes[-2], current) if len(closes) >= 2 else 0
    roc_4h = pct_change(closes[-5], current) / 4 if len(closes) >= 5 else 0  # per-hour rate
    roc_12h = pct_change(closes[-13], current) / 12 if len(closes) >= 13 else 0  # per-hour rate

    # Acceleration: is the short-term rate faster than the long-term rate?
    # Higher ratio = accelerating
    if roc_12h != 0 and roc_12h > 0:
        accel_ratio = roc_1h / roc_12h if roc_12h != 0 else 0
    elif roc_4h > 0:
        accel_ratio = roc_1h / roc_4h if roc_4h != 0 else 0
    else:
        accel_ratio = 0

    # Also check absolute momentum
    abs_momentum = abs(roc_1h)

    # Score: combine acceleration with absolute momentum
    if accel_ratio <= 0:
        accel_score = 0
    elif accel_ratio <= PRICE_ACCEL_THRESHOLD:
        accel_score = (accel_ratio / PRICE_ACCEL_THRESHOLD) * 40
    else:
        accel_score = min(100, 40 + (accel_ratio - PRICE_ACCEL_THRESHOLD) * 15)

    momentum_score = min(50, abs_momentum * 5)  # 10% move = 50 pts
    score = min(100, accel_score * 0.6 + momentum_score * 0.4)

    return {
        "score": round(score, 1),
        "roc_1h": round(roc_1h, 3),
        "roc_4h": round(roc_4h, 3),
        "roc_12h": round(roc_12h, 3),
        "accel_ratio": round(accel_ratio, 2),
        "detail": f"1h {roc_1h:+.2f}% | 4h/hr {roc_4h:+.3f}% | accel {accel_ratio:.1f}x",
    }


def analyze_oi_surge(oi_data: list[dict]) -> dict:
    """
    Detect open interest increases — new money entering.
    Compare recent OI to 4h-ago OI.
    """
    if len(oi_data) < 2:
        return {"score": 0, "oi_change_pct": 0, "detail": "insufficient data"}

    # OI data comes newest-first from API
    try:
        recent_oi = float(oi_data[0].get("openInterest", 0))
        oldest_oi = float(oi_data[-1].get("openInterest", 0))
    except (ValueError, TypeError):
        return {"score": 0, "oi_change_pct": 0, "detail": "parse error"}

    if oldest_oi <= 0:
        return {"score": 0, "oi_change_pct": 0, "detail": "no baseline OI"}

    oi_change_pct = ((recent_oi - oldest_oi) / oldest_oi) * 100

    # Score: 0 at 0%, 50 at 5%, 80 at 15%, 100 at 30%+
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
        "recent_oi": recent_oi,
        "oldest_oi": oldest_oi,
        "detail": f"OI {oi_change_pct:+.1f}% over ~4h",
    }


def analyze_funding_shift(funding_data: list[dict]) -> dict:
    """
    Detect rapid funding rate changes — traders piling in.
    """
    if len(funding_data) < 2:
        return {"score": 0, "current_rate": 0, "rate_change": 0, "detail": "insufficient data"}

    try:
        rates = [float(f.get("fundingRate", 0)) for f in funding_data]
    except (ValueError, TypeError):
        return {"score": 0, "current_rate": 0, "rate_change": 0, "detail": "parse error"}

    current_rate = rates[0] * 100  # newest first, as percentage
    avg_rate = sum(rates[1:]) / len(rates[1:]) * 100 if len(rates) > 1 else 0

    rate_change = current_rate - avg_rate
    abs_rate = abs(current_rate)

    # Score: high absolute funding OR rapid shift both matter
    shift_score = min(50, abs(rate_change) / 0.05 * 50)  # 0.05% shift = 50 pts
    level_score = min(50, abs_rate / 0.1 * 50)            # 0.1% funding = 50 pts
    score = min(100, shift_score + level_score)

    direction = "longs pay" if current_rate > 0 else "shorts pay"

    return {
        "score": round(score, 1),
        "current_rate": round(current_rate, 4),
        "avg_rate": round(avg_rate, 4),
        "rate_change": round(rate_change, 4),
        "detail": f"{current_rate:+.4f}% ({direction}) | shift {rate_change:+.4f}%",
    }


def analyze_streak(klines: list[list]) -> dict:
    """
    Count consecutive green candles with rising volume at the tail.
    Sustained momentum = higher conviction.
    """
    if len(klines) < 3:
        return {"score": 0, "green_streak": 0, "vol_rising_streak": 0, "detail": "insufficient data"}

    # Count consecutive green candles from most recent going back
    green_streak = 0
    for k in reversed(klines):
        close, open_ = float(k[4]), float(k[1])
        if close > open_:
            green_streak += 1
        else:
            break

    # Count consecutive volume increases from most recent
    vols = [float(k[5]) for k in klines]
    vol_rising = 0
    for i in range(len(vols) - 1, 0, -1):
        if vols[i] > vols[i - 1]:
            vol_rising += 1
        else:
            break

    # Score: green streak + volume rising streak
    green_score = min(60, green_streak * 10)           # 6+ candles = 60 pts
    vol_score = min(40, vol_rising * 8)                # 5+ rising = 40 pts
    score = min(100, green_score + vol_score)

    return {
        "score": round(score, 1),
        "green_streak": green_streak,
        "vol_rising_streak": vol_rising,
        "detail": f"{green_streak} green candles, {vol_rising} rising-vol bars",
    }


def analyze_squeeze_setup(funding_data: list[dict], klines: list[list],
                          oi_data: list[dict], ls_ratio: list[dict]) -> dict:
    """
    Detect the 'crime coin squeeze setup' described in @au_xbt's tweet:
      - Funding is NEGATIVE (shorts are paying longs → crowd is short)
      - OI is RISING (new short positions stacking)
      - Price is rising or stable (shorts underwater = squeeze fuel)
      - Long/short account ratio dropping while price holds = retail shorting into strength

    When all four align, market-makers often blow the shorts out with a coordinated rip.
    This scores the SETUP (pre-pump) rather than the pump itself — the earlier the better.
    """
    if len(funding_data) < 1 or len(klines) < 6 or len(oi_data) < 2:
        return {"score": 0, "detail": "insufficient data"}

    try:
        current_funding = float(funding_data[0].get("fundingRate", 0)) * 100
        recent_oi = float(oi_data[0].get("openInterest", 0))
        oldest_oi = float(oi_data[-1].get("openInterest", 0))
        closes = [float(k[4]) for k in klines]
    except (ValueError, TypeError):
        return {"score": 0, "detail": "parse error"}

    if oldest_oi <= 0:
        return {"score": 0, "detail": "no OI baseline"}

    oi_change = ((recent_oi - oldest_oi) / oldest_oi) * 100
    roc_6h = ((closes[-1] - closes[-6]) / closes[-6]) * 100 if closes[-6] > 0 else 0

    # Component scores
    # 1. Negative funding (shorts paying) — stronger negative = bigger setup
    funding_score = 0
    if current_funding < 0:
        funding_score = min(40, abs(current_funding) / 0.05 * 40)  # -0.05% funding = 40 pts

    # 2. OI rising — new shorts piling in while price holds
    oi_score = 0
    if oi_change > 0:
        oi_score = min(30, oi_change / 10 * 30)  # +10% OI = 30 pts

    # 3. Price stable-to-rising (NOT already dumping)
    price_score = 0
    if -2 <= roc_6h <= 15:  # sweet spot: flat to modestly up (not yet pumped)
        price_score = 30
    elif 15 < roc_6h <= 30:  # still usable but squeeze already starting
        price_score = 20
    elif roc_6h < -2:
        price_score = 0

    # 4. Long/short ratio bonus — if we can see retail shorting, add up to 20 pts
    ls_bonus = 0
    if ls_ratio and len(ls_ratio) >= 2:
        try:
            # Bybit returns newest first: buyRatio + sellRatio (sum = 1)
            newest = float(ls_ratio[0].get("buyRatio", 0.5))
            oldest = float(ls_ratio[-1].get("buyRatio", 0.5))
            # buyRatio dropping (more shorts) while price holds = classic setup
            if newest < oldest and newest < 0.5:
                ls_bonus = min(20, (0.5 - newest) * 100)
        except (ValueError, TypeError):
            pass

    score = min(100, funding_score + oi_score + price_score + ls_bonus)

    signals_found = []
    if current_funding < 0:
        signals_found.append(f"fund {current_funding:+.4f}%")
    if oi_change > 0:
        signals_found.append(f"OI {oi_change:+.1f}%")
    if ls_bonus > 0:
        signals_found.append("retail short")

    detail = " + ".join(signals_found) if signals_found else "no setup"

    return {
        "score": round(score, 1),
        "funding": round(current_funding, 4),
        "oi_change_pct": round(oi_change, 2),
        "roc_6h": round(roc_6h, 2),
        "ls_bonus": round(ls_bonus, 1),
        "detail": detail,
    }


def analyze_accumulation(daily_klines: list[list], oi_data: list[dict],
                         funding_data: list[dict], turnover_24h: float) -> dict:
    """
    Detect early accumulation — the quiet buildup before a pump.

    This catches RAVE/LAB/STO coins 1-5 days before the explosive move,
    when insiders are quietly positioning and volume is just waking up.

    Signals:
      1. Volume ramp — recent 3-day avg turnover vs 14-day avg (2x+ = accumulating)
      2. OI building from low base — new interest appearing in a quiet coin
      3. Price coiling — tight range or slight grind up (not pumping yet)
      4. Negative funding on quiet coin — early shorts arriving = future fuel
      5. Turnover awakening — absolute turnover very low but growing

    Score 0-100. Higher = stronger accumulation signal.
    """
    if not daily_klines or len(daily_klines) < 5:
        return {"score": 0, "detail": "insufficient daily data", "phase": "unknown"}

    try:
        d_closes = [float(k[4]) for k in daily_klines]
        d_highs = [float(k[2]) for k in daily_klines]
        d_lows = [float(k[3]) for k in daily_klines]
        d_turnovers = [float(k[6]) for k in daily_klines]
    except (ValueError, TypeError, IndexError):
        return {"score": 0, "detail": "parse error", "phase": "unknown"}

    current = d_closes[-1]
    if current <= 0:
        return {"score": 0, "detail": "zero price", "phase": "unknown"}

    flags = []
    score = 0

    # Signal 1: Volume ramp — recent volume vs baseline
    # The key accumulation signal: turnover climbing from dead levels
    recent_3d = d_turnovers[-3:] if len(d_turnovers) >= 3 else d_turnovers[-1:]
    avg_recent = sum(recent_3d) / len(recent_3d)

    older = d_turnovers[:-3] if len(d_turnovers) > 3 else d_turnovers[:1]
    avg_baseline = sum(older) / len(older) if older else 1

    if avg_baseline > 0:
        vol_ramp = avg_recent / avg_baseline
    else:
        vol_ramp = 0

    if vol_ramp >= 5:
        score += 30
        flags.append(f"vol ramp {vol_ramp:.1f}x (strong)")
    elif vol_ramp >= 3:
        score += 25
        flags.append(f"vol ramp {vol_ramp:.1f}x")
    elif vol_ramp >= 2:
        score += 15
        flags.append(f"vol ramp {vol_ramp:.1f}x (early)")
    elif vol_ramp >= 1.5:
        score += 8
        flags.append(f"vol ramp {vol_ramp:.1f}x (slight)")

    # Signal 2: OI building from low base
    # Rising OI on a quiet coin = new positions being opened
    if len(oi_data) >= 6:
        try:
            recent_oi = float(oi_data[0].get("openInterest", 0))
            oldest_oi = float(oi_data[-1].get("openInterest", 0))
            if oldest_oi > 0:
                oi_ramp = (recent_oi - oldest_oi) / oldest_oi * 100
                if oi_ramp >= 30:
                    score += 25
                    flags.append(f"OI +{oi_ramp:.0f}% (building fast)")
                elif oi_ramp >= 15:
                    score += 18
                    flags.append(f"OI +{oi_ramp:.0f}% (building)")
                elif oi_ramp >= 5:
                    score += 10
                    flags.append(f"OI +{oi_ramp:.0f}%")
        except (ValueError, TypeError):
            pass

    # Signal 3: Price coiling — tight daily range, not already pumped
    # Accumulation happens during quiet, boring price action
    recent_5d_high = max(d_highs[-5:]) if len(d_highs) >= 5 else max(d_highs)
    recent_5d_low = min(d_lows[-5:]) if len(d_lows) >= 5 else min(d_lows)
    range_5d = (recent_5d_high - recent_5d_low) / recent_5d_low * 100 if recent_5d_low > 0 else 999

    if range_5d < 10:
        score += 20
        flags.append(f"coiling {range_5d:.1f}% 5d range (very tight)")
    elif range_5d < 20:
        score += 12
        flags.append(f"coiling {range_5d:.1f}% 5d range")
    elif range_5d < 30:
        score += 5
        flags.append(f"{range_5d:.1f}% 5d range")
    elif range_5d > 80:
        # Already pumped hard — not accumulation, this is mid-move
        score -= 15
        flags.append(f"already {range_5d:.0f}% 5d range (too extended)")

    # Signal 4: Negative funding on a quiet coin = shorts arriving early
    # Before a pump, smart shorts pile in because they've seen the pattern.
    # This becomes squeeze fuel when the pump starts.
    if funding_data and len(funding_data) >= 2:
        try:
            funding_rates = [float(f.get("fundingRate", 0)) * 100 for f in funding_data[:6]]
            neg_count = sum(1 for f in funding_rates if f < 0)
            avg_funding = sum(funding_rates) / len(funding_rates)

            if neg_count >= 4 and range_5d < 30:
                score += 15
                flags.append(f"fund neg {neg_count}/{len(funding_rates)}c = fuel")
            elif neg_count >= 2 and range_5d < 30:
                score += 8
                flags.append(f"fund neg {neg_count}/{len(funding_rates)}c")
        except (ValueError, TypeError):
            pass

    # Signal 5: Low absolute turnover (this IS a quiet coin, not already popular)
    # Coins with $100K-$2M turnover are the sweet spot for accumulation detection
    # Above $10M they're already on everyone's radar
    if turnover_24h < 2_000_000:
        score += 10
        flags.append(f"${turnover_24h/1e6:.1f}M turnover (under radar)")
    elif turnover_24h < 5_000_000:
        score += 5
        flags.append(f"${turnover_24h/1e6:.1f}M turnover")
    elif turnover_24h > 50_000_000:
        # High turnover = already on everyone's radar, less accumulation edge
        score -= 10

    score = max(0, min(100, score))

    if score >= 50:
        phase = "ACCUMULATING"
    elif score >= 30:
        phase = "AWAKENING"
    else:
        phase = "no_setup"

    return {
        "score": round(score, 1),
        "phase": phase,
        "vol_ramp": round(vol_ramp, 2),
        "range_5d": round(range_5d, 1),
        "turnover_24h": turnover_24h,
        "detail": " | ".join(flags) if flags else "quiet",
    }


def analyze_distribution_risk(klines: list[list], oi_data: list[dict]) -> dict:
    """
    Penalty signal: is this a late/distribution setup rather than an early entry?
    Red flags (crime coin exit signs):
      - Price is near 48h high AND already up a lot
      - OI dropping while price still high = smart money unloading
      - Very extended move (50%+) with decelerating momentum

    Returns a penalty value (0 to DISTRIBUTION_PENALTY_CAP) to subtract from score.
    """
    if len(klines) < 12 or len(oi_data) < 2:
        return {"penalty": 0, "detail": "insufficient data"}

    try:
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        recent_oi = float(oi_data[0].get("openInterest", 0))
        oldest_oi = float(oi_data[-1].get("openInterest", 0))
    except (ValueError, TypeError):
        return {"penalty": 0, "detail": "parse error"}

    current = closes[-1]
    high_48h = max(highs)
    low_12h = min(closes[-12:])

    # How extended is the move?
    move_from_low = ((current - low_12h) / low_12h * 100) if low_12h > 0 else 0

    # Distance from high (smaller = closer to top)
    dist_from_high = ((high_48h - current) / high_48h * 100) if high_48h > 0 else 0

    # OI direction
    oi_change = ((recent_oi - oldest_oi) / oldest_oi * 100) if oldest_oi > 0 else 0

    penalty = 0
    reasons = []

    # Red flag 1: already up 50%+ from 12h low
    if move_from_low >= 50:
        penalty += 15
        reasons.append(f"+{move_from_low:.0f}% from 12h low")

    # Red flag 2: within 2% of 48h high (late in the move)
    if dist_from_high <= 2 and move_from_low >= 30:
        penalty += 10
        reasons.append("at 48h high")

    # Red flag 3: OI dropping while price still elevated = distribution
    if oi_change < -3 and move_from_low >= 20:
        penalty += 15
        reasons.append(f"OI {oi_change:.1f}% (distrib)")

    penalty = min(DISTRIBUTION_PENALTY_CAP, penalty)

    return {
        "penalty": round(penalty, 1),
        "move_from_low": round(move_from_low, 1),
        "dist_from_high": round(dist_from_high, 2),
        "oi_change": round(oi_change, 2),
        "detail": " | ".join(reasons) if reasons else "clean",
    }


# Crime Pump Risk threshold — coins at or above this are hard-blocked
CRIME_PUMP_BLOCK_THRESHOLD = 60


def analyze_pre_squeeze_setup(funding_data: list[dict], klines: list[list],
                              oi_data: list[dict]) -> dict:
    """
    Detect MYX-style pre-squeeze SETUP — the trap before the rip.

    The MYX pattern (the goldmine entry):
      Phase 2: Initial bait pump (already happened)
      Phase 3: Consolidation with sustained negative funding (TRAP being set)
      Phase 4: Explosive squeeze (THE RIP — what we want to ride)

    Detects Phase 3 by requiring ALL of:
      - Funding has been negative for last 3+ cycles (sustained, not just spike)
      - Price is consolidating (tight range relative to recent move)
      - OI rising during consolidation (shorts piling in)
      - Had a recent pump (24h-7d ago) that's now ranging (the bait already happened)

    Score 0-100. Higher = stronger setup, riper for explosive squeeze.
    """
    if len(funding_data) < 4 or len(klines) < 24 or len(oi_data) < 6:
        return {"score": 0, "detail": "insufficient data", "phase": "unknown"}

    try:
        funding_rates = [float(f.get("fundingRate", 0)) * 100 for f in funding_data[:8]]
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        recent_oi = float(oi_data[0].get("openInterest", 0))
        oldest_oi = float(oi_data[-1].get("openInterest", 0))
    except (ValueError, TypeError):
        return {"score": 0, "detail": "parse error", "phase": "unknown"}

    if oldest_oi <= 0:
        return {"score": 0, "detail": "no OI baseline", "phase": "unknown"}

    current = closes[-1]

    # Signal 1: Sustained negative funding (multiple cycles)
    negative_cycles = sum(1 for f in funding_rates if f < 0)
    avg_funding = sum(funding_rates) / len(funding_rates)
    sustained_neg_score = 0
    if negative_cycles >= 6:
        sustained_neg_score = 35
    elif negative_cycles >= 4:
        sustained_neg_score = 25
    elif negative_cycles >= 3:
        sustained_neg_score = 15

    # Signal 2: Price consolidation — tight range over last 24 bars
    last_24h_range = max(closes[-24:]) - min(closes[-24:])
    last_24h_avg = sum(closes[-24:]) / 24
    consol_pct = (last_24h_range / last_24h_avg * 100) if last_24h_avg > 0 else 0
    consol_score = 0
    if consol_pct < 8:
        consol_score = 25  # very tight range
    elif consol_pct < 15:
        consol_score = 15  # moderately tight
    elif consol_pct < 25:
        consol_score = 5

    # Signal 3: OI rising during consolidation (shorts stacking)
    oi_change = (recent_oi - oldest_oi) / oldest_oi * 100
    oi_score = 0
    if oi_change > 15:
        oi_score = 25
    elif oi_change > 5:
        oi_score = 15
    elif oi_change > 0:
        oi_score = 5

    # Signal 4: Had a recent bait pump (price is well above the 48h low)
    high_48h = max(highs)
    low_48h = min(lows)
    pump_size = (high_48h - low_48h) / low_48h * 100 if low_48h > 0 else 0
    # Distance from current to high (closer to high = consolidating after pump)
    dist_from_high = (high_48h - current) / high_48h * 100 if high_48h > 0 else 100
    bait_score = 0
    if pump_size >= 40 and dist_from_high < 25:
        # Had a meaningful pump and now sitting near the high (range top)
        bait_score = 15
    elif pump_size >= 20 and dist_from_high < 30:
        bait_score = 10

    score = min(100, sustained_neg_score + consol_score + oi_score + bait_score)

    # Build detail string
    parts = []
    if sustained_neg_score > 0:
        parts.append(f"fund neg {negative_cycles}/8c (avg {avg_funding:+.4f}%)")
    if consol_score > 0:
        parts.append(f"range {consol_pct:.1f}%")
    if oi_score > 0:
        parts.append(f"OI {oi_change:+.1f}%")
    if bait_score > 0:
        parts.append(f"pump {pump_size:.0f}% / {dist_from_high:.0f}% off high")

    # Phase classification
    if score >= 60:
        phase = "TRAP_SET"  # Phase 3 — squeeze imminent
    elif score >= 40:
        phase = "ACCUMULATING"  # Phase 2-3 transition
    else:
        phase = "no_setup"

    return {
        "score": round(score, 1),
        "phase": phase,
        "negative_cycles": negative_cycles,
        "avg_funding": round(avg_funding, 4),
        "consol_pct": round(consol_pct, 2),
        "oi_change": round(oi_change, 2),
        "pump_size": round(pump_size, 1),
        "dist_from_high": round(dist_from_high, 1),
        "detail": " | ".join(parts) if parts else "no setup",
    }


def analyze_crime_pump_risk(klines: list[list], oi_data: list[dict],
                            funding_data: list[dict], turnover_24h: float,
                            daily_klines: list[list] | None = None) -> dict:
    """
    Detect crime pump manipulation patterns from @tradinghoex's playbook.

    Signals checked:
      1. Vol/OI brushing — 24h volume / OI > 20x = likely fake volume
      2. Extreme negative funding — shorts paying > 0.05% = squeeze bait trap
      3. Parabolic run — 100%+ in 24h or 200%+ in 48h = you're the exit liquidity
      4. OI outsized vs turnover — OI > 2x daily turnover on a low-cap = manipulation
      5. Squeeze phase — negative funding + rising OI + rising price = active squeeze
      6. Multi-day parabolic — 200%+ over 7d or 500%+ over 14d (RAVE/LAB pattern)
      7. Derivatives frenzy — positive funding + extreme volume = sell-the-news trap

    Returns crime_score (0-100). >= CRIME_PUMP_BLOCK_THRESHOLD = SKIP this coin.
    """
    if len(klines) < 12 or len(oi_data) < 2 or len(funding_data) < 1:
        return {"crime_score": 0, "blocked": False, "detail": "insufficient data", "flags": []}

    try:
        closes = [float(k[4]) for k in klines]
        current = closes[-1]
        low_24h = min(closes[-24:]) if len(closes) >= 24 else min(closes)
        low_48h = min(closes)
        recent_oi = float(oi_data[0].get("openInterest", 0))
        current_funding = float(funding_data[0].get("fundingRate", 0)) * 100
    except (ValueError, TypeError, IndexError):
        return {"crime_score": 0, "blocked": False, "detail": "parse error", "flags": []}

    flags = []
    crime_score = 0

    # 1. Vol/OI Brushing — fake volume detection
    # Normal range is 3-8x, above 20x = likely brushed
    if recent_oi > 0 and turnover_24h > 0:
        vol_oi_ratio = turnover_24h / recent_oi
        if vol_oi_ratio > 20:
            pts = min(25, (vol_oi_ratio - 20) / 10 * 25)
            crime_score += pts
            flags.append(f"Vol/OI {vol_oi_ratio:.0f}x (brushed)")
        elif vol_oi_ratio > 12:
            pts = min(10, (vol_oi_ratio - 12) / 8 * 10)
            crime_score += pts
            flags.append(f"Vol/OI {vol_oi_ratio:.0f}x (elevated)")

    # 2. Extreme negative funding — only flag if it's the SQUEEZE peak, not the setup
    # Deeply negative + price already pumped 50%+ in 24h = squeeze in progress (late entry)
    # Deeply negative + price flat/consolidating = SETUP (we want to enter — handled by pre_squeeze)
    move_24h_check = ((current - min(closes[-24:])) / min(closes[-24:]) * 100) if len(closes) >= 24 and min(closes[-24:]) > 0 else 0
    if current_funding < -0.05 and move_24h_check > 50:
        # This is the squeeze peak, not the setup — penalize
        pts = min(20, abs(current_funding) / 0.1 * 20)
        crime_score += pts
        flags.append(f"funding {current_funding:+.4f}% during +{move_24h_check:.0f}% pump (late)")

    # 3. Parabolic run — only flag as DISTRIBUTION if signals confirm exit phase
    # Active squeeze (negative funding + rising OI + rising price) is RIDEABLE
    # Distribution (funding flipped positive OR OI dropping while price up) is the trap
    move_24h = ((current - low_24h) / low_24h * 100) if low_24h > 0 else 0
    move_48h = ((current - low_48h) / low_48h * 100) if low_48h > 0 else 0

    # Determine if squeeze is still active or distribution has begun
    oi_change_for_phase = 0
    if len(oi_data) >= 2:
        try:
            old_oi = float(oi_data[-1].get("openInterest", 0))
            if old_oi > 0:
                oi_change_for_phase = (recent_oi - old_oi) / old_oi * 100
        except (ValueError, TypeError):
            pass

    # Squeeze still alive: funding negative AND OI rising AND price rising
    squeeze_active = current_funding < -0.01 and oi_change_for_phase > 5

    # Distribution phase: parabolic but funding positive OR OI dropping
    in_distribution = current_funding > 0.01 or oi_change_for_phase < -3

    if move_24h >= 200:
        if in_distribution:
            crime_score += 30
            flags.append(f"+{move_24h:.0f}% in 24h, distribution phase")
        elif squeeze_active:
            crime_score += 5  # mild penalty — squeeze still rideable but late
            flags.append(f"+{move_24h:.0f}% in 24h, squeeze active (late)")
        else:
            crime_score += 20
            flags.append(f"+{move_24h:.0f}% in 24h (parabolic)")
    elif move_24h >= 100:
        if in_distribution:
            crime_score += 20
            flags.append(f"+{move_24h:.0f}% in 24h, distribution")
        elif not squeeze_active:
            crime_score += 12
            flags.append(f"+{move_24h:.0f}% in 24h (extended)")
        # if squeeze_active, don't penalize — riding the rip
    elif move_48h >= 200 and in_distribution:
        crime_score += 15
        flags.append(f"+{move_48h:.0f}% in 48h, distribution")

    # 4. OI outsized relative to daily turnover
    # If OI is 2x+ daily turnover, positions are way larger than real trading activity
    if turnover_24h > 0 and recent_oi > 0:
        oi_turnover_ratio = recent_oi / turnover_24h
        if oi_turnover_ratio > 3:
            pts = min(15, (oi_turnover_ratio - 3) * 5)
            crime_score += pts
            flags.append(f"OI {oi_turnover_ratio:.1f}x turnover (outsized)")

    # 5. Active squeeze pattern: negative funding + rising OI + rising price
    # This is the exact MYX playbook — you're entering mid-squeeze
    if len(oi_data) >= 2:
        oldest_oi = float(oi_data[-1].get("openInterest", 0))
        oi_change = ((recent_oi - oldest_oi) / oldest_oi * 100) if oldest_oi > 0 else 0

        roc_6h = ((closes[-1] - closes[-6]) / closes[-6] * 100) if len(closes) >= 6 and closes[-6] > 0 else 0

        if current_funding < -0.02 and oi_change > 10 and roc_6h > 10:
            crime_score += 15
            flags.append("active squeeze (fund- OI+ price+)")

    # 6. Multi-day parabolic (RAVE/LAB pattern) — catches coins already up 200%+ over days
    # These coins have already done the massive run; entering now = exit liquidity
    if daily_klines and len(daily_klines) >= 3:
        try:
            daily_closes = [float(k[4]) for k in daily_klines]
            daily_lows = [float(k[3]) for k in daily_klines]
            current_d = daily_closes[-1]

            # 7-day move (or available)
            lookback_7d = min(7, len(daily_lows))
            low_7d = min(daily_lows[-lookback_7d:])
            move_7d = ((current_d - low_7d) / low_7d * 100) if low_7d > 0 else 0

            # 14-day move (or available)
            low_14d = min(daily_lows)
            move_14d = ((current_d - low_14d) / low_14d * 100) if low_14d > 0 else 0

            if move_14d >= 500:
                if in_distribution:
                    crime_score += 30
                    flags.append(f"+{move_14d:.0f}% in 14d, distribution (LAB pattern)")
                elif squeeze_active:
                    crime_score += 10
                    flags.append(f"+{move_14d:.0f}% in 14d, squeeze active")
                else:
                    crime_score += 25
                    flags.append(f"+{move_14d:.0f}% in 14d (extreme parabolic)")
            elif move_7d >= 200:
                if in_distribution:
                    crime_score += 25
                    flags.append(f"+{move_7d:.0f}% in 7d, distribution")
                elif not squeeze_active:
                    crime_score += 15
                    flags.append(f"+{move_7d:.0f}% in 7d (extended)")
        except (ValueError, TypeError, IndexError):
            pass

    # 7. Derivatives frenzy — positive funding + extreme 24h volume surge
    # When funding flips heavily positive after a big run, the longs are crowded
    # and market makers are about to dump on them (LAB app-launch sell-the-news)
    if current_funding > 0.05 and move_24h >= 50:
        pts = min(20, current_funding / 0.1 * 15)
        crime_score += pts
        flags.append(f"fund {current_funding:+.4f}% + {move_24h:.0f}% pump (frenzy top)")

    crime_score = min(100, crime_score)
    blocked = crime_score >= CRIME_PUMP_BLOCK_THRESHOLD

    return {
        "crime_score": round(crime_score, 1),
        "blocked": blocked,
        "flags": flags,
        "detail": " | ".join(flags) if flags else "clean",
    }


# ──────────────────────────────────────────────
# Composite scoring
# ──────────────────────────────────────────────

def compute_momentum_score(signals: dict) -> float:
    """
    Weighted composite score from all signals, minus distribution penalty.
    Penalty weeds out late/distribution entries (the kind that trap you at the top).
    """
    score = 0
    for key, weight in WEIGHTS.items():
        signal = signals.get(key, {})
        score += signal.get("score", 0) * weight

    penalty = signals.get("distribution_risk", {}).get("penalty", 0)
    final = max(0, score - penalty)
    return round(final, 1)


# ──────────────────────────────────────────────
# Main scanner
# ──────────────────────────────────────────────

def run_scan(base_url: str, top_n: int = 20, min_score: float = 0) -> list[dict]:
    """Run a full momentum scan across all linear perpetuals."""
    global _call_count
    _call_count = 0

    print(f"\n  Fetching all linear perpetual tickers...")
    tickers = fetch_all_linear_tickers(base_url)
    if not tickers:
        print("  Failed to fetch tickers.")
        return []

    # Pre-filter: positive 24h change, minimum liquidity, USDT pairs
    candidates = []
    for t in tickers:
        try:
            symbol = t["symbol"]
            change_pct = float(t.get("price24hPcnt", 0)) * 100
            turnover_24h = float(t.get("turnover24h", 0))
            last_price = float(t.get("lastPrice", 0))
            volume_24h = float(t.get("volume24h", 0))
        except (ValueError, TypeError, KeyError):
            continue

        if not symbol.endswith("USDT"):
            continue
        # Note: don't filter by % change here — MYX-style trap setups have flat 24h%
        # but are the goldmine entries. Filter happens via scoring instead.
        if turnover_24h < MIN_TURNOVER_24H:
            continue
        if last_price <= 0:
            continue

        candidates.append({
            "symbol": symbol,
            "lastPrice": last_price,
            "change24h": change_pct,
            "turnover24h": turnover_24h,
            "volume24h": volume_24h,
        })

    # Sort by 24h turnover to prioritize liquid coins for deep analysis
    candidates.sort(key=lambda x: x["turnover24h"], reverse=True)

    # Four-pool approach to catch all setup types:
    #   Pool A: Top 50 by 24h volume (established, liquid coins)
    #   Pool B: Top 30 by 24h % change (fast movers — catches active spikes)
    #   Pool C: Top 30 flat-to-down with elevated turnover (MYX-style trap candidates)
    #   Pool D: Top 30 quiet/low-turnover coins (accumulation candidates —
    #           these are the RAVE/LAB/STO coins before anyone notices them)
    pool_a = candidates[:50]
    pool_a_symbols = {c["symbol"] for c in pool_a}

    # Pool B: sort by % change desc (skip negatives), exclude Pool A
    remaining_b = [c for c in candidates if c["symbol"] not in pool_a_symbols
                   and c["turnover24h"] >= 1_000_000 and c["change24h"] >= 1.0]
    remaining_b.sort(key=lambda x: x["change24h"], reverse=True)
    pool_b = remaining_b[:30]
    pool_b_symbols = {c["symbol"] for c in pool_b}

    # Pool C: flat to mildly down coins with decent turnover (potential trap setups)
    flat_candidates = [c for c in candidates
                       if c["symbol"] not in pool_a_symbols
                       and c["symbol"] not in pool_b_symbols
                       and c["turnover24h"] >= 2_000_000
                       and -10 <= c["change24h"] <= 5]
    flat_candidates.sort(key=lambda x: x["turnover24h"], reverse=True)
    pool_c = flat_candidates[:30]
    pool_c_symbols = {c["symbol"] for c in pool_c}

    # Pool D: quiet low-turnover coins — the accumulation sweet spot
    # $100K-$5M daily turnover, any price direction, not in other pools
    # These are under-the-radar coins where volume may be just starting to wake up
    quiet_candidates = [c for c in candidates
                        if c["symbol"] not in pool_a_symbols
                        and c["symbol"] not in pool_b_symbols
                        and c["symbol"] not in pool_c_symbols
                        and c["turnover24h"] <= 5_000_000]
    quiet_candidates.sort(key=lambda x: x["turnover24h"], reverse=True)
    pool_d = quiet_candidates[:30]

    scan_pool = pool_a + pool_b + pool_c + pool_d

    print(f"  {len(tickers)} perps found → {len(candidates)} candidates after filters")
    print(f"  Scan pool: {len(pool_a)} volume + {len(pool_b)} movers + {len(pool_c)} flat + {len(pool_d)} quiet (accumulation) = {len(scan_pool)} coins")
    print()

    results = []
    for i, c in enumerate(scan_pool):
        symbol = c["symbol"]
        pct = (i + 1) / len(scan_pool) * 100
        sys.stdout.write(f"\r  Scanning [{i+1}/{len(scan_pool)}] {symbol:<16} ({pct:.0f}%)")
        sys.stdout.flush()

        # Fetch detailed data
        klines = fetch_klines(base_url, symbol, interval="60", limit=48)  # 48h of 1h candles
        daily_klines = fetch_daily_klines(base_url, symbol, limit=14)  # 14 days
        oi_data = fetch_open_interest(base_url, symbol)
        funding_data = fetch_funding_history(base_url, symbol)
        ls_ratio = fetch_long_short_ratio(base_url, symbol)

        # Run all signal analyzers
        signals = {
            "volume_anomaly": analyze_volume_anomaly(klines),
            "price_accel": analyze_price_acceleration(klines),
            "oi_surge": analyze_oi_surge(oi_data),
            "funding_shift": analyze_funding_shift(funding_data),
            "streak": analyze_streak(klines),
            "squeeze_setup": analyze_squeeze_setup(funding_data, klines, oi_data, ls_ratio),
            "pre_squeeze": analyze_pre_squeeze_setup(funding_data, klines, oi_data),
            "accumulation": analyze_accumulation(daily_klines, oi_data, funding_data, c["turnover24h"]),
            "distribution_risk": analyze_distribution_risk(klines, oi_data),
            "crime_pump": analyze_crime_pump_risk(klines, oi_data, funding_data,
                                                  c["turnover24h"], daily_klines),
        }

        # Hard block: skip coins flagged as crime pumps
        if signals["crime_pump"]["blocked"]:
            continue

        momentum_score = compute_momentum_score(signals)

        if momentum_score >= min_score:
            results.append({
                **c,
                "momentum_score": momentum_score,
                "signals": signals,
            })

    print(f"\r  Scanning complete. {_call_count} API calls made.{' ' * 40}")

    # Sort by momentum score
    results.sort(key=lambda x: x["momentum_score"], reverse=True)
    return results[:top_n]


def format_alert_level(score: float) -> str:
    """Return an alert label based on momentum score."""
    if score >= 80:
        return "!! EXTREME"
    elif score >= 60:
        return "!  HIGH"
    elif score >= 40:
        return "~  ELEVATED"
    elif score >= 25:
        return "   MODERATE"
    else:
        return "   LOW"


def display_results(results: list[dict], env_label: str):
    """Print formatted scanner results."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"\n{'='*130}")
    print(f"[{env_label}] MOMENTUM SCANNER — {now}")
    print(f"{'='*130}")

    if not results:
        print("\n  No coins met the scoring threshold.\n")
        return

    # Header
    print(
        f"{'#':>3}  {'Symbol':<14} {'Price':>12} {'24h%':>8} {'Score':>6} {'Alert':<12}"
        f"  {'Accum':>5} {'Vol':>5} {'OI':>5} {'Sqz':>5} {'PreSq':>5} {'PrAc':>5} {'Strk':>5} {'Pen':>5} {'Crime':>5}"
        f"  {'Phase':<14}  {'Key Signal':<30}"
    )
    print("-" * 175)

    for i, r in enumerate(results, 1):
        s = r["signals"]
        acc_s = s["accumulation"]["score"]
        vol_s = s["volume_anomaly"]["score"]
        pa_s = s["price_accel"]["score"]
        oi_s = s["oi_surge"]["score"]
        sq_s = s["squeeze_setup"]["score"]
        ps_s = s["pre_squeeze"]["score"]
        st_s = s["streak"]["score"]
        pen = s["distribution_risk"]["penalty"]
        crime = s["crime_pump"]["crime_score"]

        # Show the most relevant phase
        acc_phase = s["accumulation"].get("phase", "—")
        pre_phase = s["pre_squeeze"].get("phase", "—")
        if acc_phase in ("ACCUMULATING", "AWAKENING"):
            phase = acc_phase
        elif pre_phase in ("TRAP_SET", "ACCUMULATING"):
            phase = pre_phase
        else:
            phase = "—"

        # Pick the strongest signal as the key signal
        signal_scores = [
            ("accumulation", acc_s),
            ("volume_anomaly", vol_s),
            ("price_accel", pa_s),
            ("oi_surge", oi_s),
            ("squeeze_setup", sq_s),
            ("pre_squeeze", ps_s),
            ("streak", st_s),
        ]
        top_signal_name, _ = max(signal_scores, key=lambda x: x[1])
        key_signal = s[top_signal_name]["detail"]

        alert = format_alert_level(r["momentum_score"])

        print(
            f"{i:>3}  {r['symbol']:<14} {r['lastPrice']:>12,.6g} {r['change24h']:>+7.1f}% {r['momentum_score']:>5.1f} {alert:<12}"
            f"  {acc_s:>5.0f} {vol_s:>5.0f} {oi_s:>5.0f} {sq_s:>5.0f} {ps_s:>5.0f} {pa_s:>5.0f} {st_s:>5.0f} {-pen:>5.0f} {crime:>5.0f}"
            f"  {phase:<14}  {key_signal:<30}"
        )

    print("-" * 165)

    # Detail section for top 5
    print(f"\n{'─'*60}")
    print(f"DETAILED BREAKDOWN — Top {min(5, len(results))}")
    print(f"{'─'*60}")

    for r in results[:5]:
        s = r["signals"]
        pen = s["distribution_risk"]["penalty"]
        pen_str = f"  [PENALTY -{pen:.0f}]" if pen > 0 else ""
        print(f"\n  {r['symbol']}  (Score: {r['momentum_score']}){pen_str}  24h: {r['change24h']:+.1f}%")
        acc = s["accumulation"]
        if acc["score"] > 0:
            print(f"    ACCUM:    {acc['detail']} (score {acc['score']}, phase: {acc['phase']})")
        print(f"    Volume:   {s['volume_anomaly']['detail']}")
        print(f"    PrAccel:  {s['price_accel']['detail']}")
        print(f"    OI Surge: {s['oi_surge']['detail']}")
        print(f"    Funding:  {s['funding_shift']['detail']}")
        print(f"    Streak:   {s['streak']['detail']}")
        print(f"    Squeeze:  {s['squeeze_setup']['detail']} (score {s['squeeze_setup']['score']})")
        ps = s["pre_squeeze"]
        if ps["score"] > 0:
            print(f"    PreSqz:   {ps['detail']} (score {ps['score']}, phase: {ps['phase']})")
        print(f"    DistRisk: {s['distribution_risk']['detail']}")
        crime = s["crime_pump"]
        if crime["crime_score"] > 0:
            print(f"    Crime:    {crime['detail']} (score {crime['crime_score']})")

    print(f"\n{'─'*60}")
    print(f"Score 80+ = EXTREME momentum (coin may already be spiking)")
    print(f"Score 60+ = HIGH momentum (strong pre-spike signals)")
    print(f"Score 40+ = ELEVATED (building momentum, watch closely)")
    print(f"Score 25+ = MODERATE (early signs, needs confirmation)")
    print()


def save_results(results: list[dict], filename: str = "momentum_scan.json"):
    """Save raw results to JSON."""
    with open(filename, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Raw data saved to {filename}")


def main():
    parser = argparse.ArgumentParser(
        description="Bybit Momentum Scanner — detect coins before they spike"
    )
    parser.add_argument("--testnet", action="store_true", help="Use testnet")
    parser.add_argument("--top", type=int, default=20, help="Show top N results (default: 20)")
    parser.add_argument("--min-score", type=float, default=0, help="Minimum momentum score to display")
    parser.add_argument("--watch", type=int, default=0, help="Rescan interval in minutes (0 = one-shot)")
    parser.add_argument("--save", action="store_true", help="Save results to JSON file")
    args = parser.parse_args()

    base_url = TESTNET_URL if args.testnet else MAINNET_URL
    env_label = "TESTNET" if args.testnet else "MAINNET"

    print(f"\n[{env_label}] Bybit Momentum Scanner")
    print(f"  Weights: Vol={WEIGHTS['volume_anomaly']:.0%} PrAcc={WEIGHTS['price_accel']:.0%} "
          f"OI={WEIGHTS['oi_surge']:.0%} Squeeze={WEIGHTS['squeeze_setup']:.0%} Streak={WEIGHTS['streak']:.0%}")
    print(f"  Distribution penalty: up to -{DISTRIBUTION_PENALTY_CAP:.0f} pts for late/top-heavy setups")
    print(f"  Filters: min turnover ${MIN_TURNOVER_24H:,} | min change {MIN_PRICE_CHANGE_PCT}% | USDT pairs only")

    while True:
        results = run_scan(base_url, top_n=args.top, min_score=args.min_score)
        display_results(results, env_label)

        if args.save:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            save_results(results, f"momentum_scan_{ts}.json")

        if args.watch <= 0:
            break

        print(f"  Next scan in {args.watch} minutes... (Ctrl+C to stop)\n")
        try:
            time.sleep(args.watch * 60)
        except KeyboardInterrupt:
            print("\n  Scanner stopped.")
            break


if __name__ == "__main__":
    main()
