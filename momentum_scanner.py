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
    "volume_anomaly": 0.30,
    "price_accel":    0.25,
    "oi_surge":       0.20,
    "funding_shift":  0.10,
    "streak":         0.15,
}

# Thresholds
VOLUME_ALERT_MULTIPLIER = 3.0     # 3x average volume = notable
VOLUME_EXTREME_MULTIPLIER = 8.0   # 8x+ = extreme
OI_CHANGE_ALERT_PCT = 5.0         # 5% OI increase in recent window
PRICE_ACCEL_THRESHOLD = 1.5       # acceleration ratio threshold
MIN_TURNOVER_24H = 500_000        # skip low-liquidity coins (< $500k)
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
    except urllib.error.URLError as e:
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


# ──────────────────────────────────────────────
# Composite scoring
# ──────────────────────────────────────────────

def compute_momentum_score(signals: dict) -> float:
    """Weighted composite score from all signals."""
    score = 0
    for key, weight in WEIGHTS.items():
        signal = signals.get(key, {})
        score += signal.get("score", 0) * weight
    return round(score, 1)


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
        if change_pct < MIN_PRICE_CHANGE_PCT:
            continue
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

    # Two-pool approach to catch both liquid movers AND emerging spikes:
    #   Pool A: Top 50 by 24h volume (established, liquid coins)
    #   Pool B: Top 30 by 24h % change (fast movers — catches early spikes
    #           that don't yet have huge absolute volume)
    pool_a = candidates[:50]
    pool_a_symbols = {c["symbol"] for c in pool_a}

    # Pool B: sort by % change, exclude coins already in Pool A, require min $1M turnover
    remaining = [c for c in candidates if c["symbol"] not in pool_a_symbols
                 and c["turnover24h"] >= 1_000_000]
    remaining.sort(key=lambda x: x["change24h"], reverse=True)
    pool_b = remaining[:30]

    scan_pool = pool_a + pool_b

    print(f"  {len(tickers)} perps found → {len(candidates)} candidates after filters")
    print(f"  Scan pool: {len(pool_a)} by volume + {len(pool_b)} by % change = {len(scan_pool)} coins")
    print()

    results = []
    for i, c in enumerate(scan_pool):
        symbol = c["symbol"]
        pct = (i + 1) / len(scan_pool) * 100
        sys.stdout.write(f"\r  Scanning [{i+1}/{len(scan_pool)}] {symbol:<16} ({pct:.0f}%)")
        sys.stdout.flush()

        # Fetch detailed data
        klines = fetch_klines(base_url, symbol, interval="60", limit=48)  # 48h of 1h candles
        oi_data = fetch_open_interest(base_url, symbol)
        funding_data = fetch_funding_history(base_url, symbol)

        # Run all signal analyzers
        signals = {
            "volume_anomaly": analyze_volume_anomaly(klines),
            "price_accel": analyze_price_acceleration(klines),
            "oi_surge": analyze_oi_surge(oi_data),
            "funding_shift": analyze_funding_shift(funding_data),
            "streak": analyze_streak(klines),
        }

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
        f"  {'Vol':>6} {'PrAcc':>6} {'OI':>6} {'Fund':>6} {'Strk':>6}"
        f"  {'Key Signal':<30}"
    )
    print("-" * 130)

    for i, r in enumerate(results, 1):
        s = r["signals"]
        vol_s = s["volume_anomaly"]["score"]
        pa_s = s["price_accel"]["score"]
        oi_s = s["oi_surge"]["score"]
        fn_s = s["funding_shift"]["score"]
        st_s = s["streak"]["score"]

        # Pick the strongest signal as the key signal
        signal_scores = [
            ("volume_anomaly", vol_s),
            ("price_accel", pa_s),
            ("oi_surge", oi_s),
            ("funding_shift", fn_s),
            ("streak", st_s),
        ]
        top_signal_name, _ = max(signal_scores, key=lambda x: x[1])
        key_signal = s[top_signal_name]["detail"]

        alert = format_alert_level(r["momentum_score"])

        print(
            f"{i:>3}  {r['symbol']:<14} {r['lastPrice']:>12,.6g} {r['change24h']:>+7.1f}% {r['momentum_score']:>5.1f} {alert:<12}"
            f"  {vol_s:>5.0f} {pa_s:>5.0f} {oi_s:>5.0f} {fn_s:>5.0f} {st_s:>5.0f}"
            f"  {key_signal:<30}"
        )

    print("-" * 130)

    # Detail section for top 5
    print(f"\n{'─'*60}")
    print(f"DETAILED BREAKDOWN — Top {min(5, len(results))}")
    print(f"{'─'*60}")

    for r in results[:5]:
        s = r["signals"]
        print(f"\n  {r['symbol']}  (Score: {r['momentum_score']})  24h: {r['change24h']:+.1f}%")
        print(f"    Volume:   {s['volume_anomaly']['detail']}")
        print(f"    PrAccel:  {s['price_accel']['detail']}")
        print(f"    OI Surge: {s['oi_surge']['detail']}")
        print(f"    Funding:  {s['funding_shift']['detail']}")
        print(f"    Streak:   {s['streak']['detail']}")

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
          f"OI={WEIGHTS['oi_surge']:.0%} Fund={WEIGHTS['funding_shift']:.0%} Streak={WEIGHTS['streak']:.0%}")
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
