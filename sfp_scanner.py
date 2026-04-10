#!/usr/bin/env python3
"""
Bybit Swing Failure Pattern (SFP) Scanner

Scans high-volume Bybit perpetual contracts for Swing Failure Patterns:

  Bearish SFP: Wick sweeps above a swing high, but candle closes below it.
               → Liquidity was grabbed above the high, sellers stepped in.

  Bullish SFP: Wick sweeps below a swing low, but candle closes above it.
               → Liquidity was grabbed below the low, buyers stepped in.

Only scans coins with 24h turnover > $50M (configurable) to focus on
liquid, structured markets where SFPs are most reliable.

Usage:
    python3 sfp_scanner.py                     # one-shot scan (1h + 4h)
    python3 sfp_scanner.py --watch 30          # rescan every 30 min
    python3 sfp_scanner.py --timeframe 4h      # only scan 4h timeframe
    python3 sfp_scanner.py --min-volume 100    # min $100M volume
    python3 sfp_scanner.py --lookback 7        # 7-bar pivot lookback
    python3 sfp_scanner.py --testnet           # use testnet

No API key required — all endpoints are public.
"""

from __future__ import annotations

import argparse
import json
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

    print(f"\n[{env_label}] Bybit SFP Scanner")
    print(f"  Timeframes:    {', '.join(timeframes)}")
    print(f"  Min volume:    ${args.min_volume}M")
    print(f"  Pivot lookback: {args.lookback} bars")
    print(f"  Min sweep:     {args.min_sweep}%")
    print(f"  Max candles ago: {args.max_ago}")

    while True:
        results = run_sfp_scan(
            base_url, timeframes, args.min_volume, args.lookback,
            args.min_sweep, args.max_ago,
        )
        display_results(results, env_label, timeframes)

        if args.save:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            save_results(results, f"sfp_scan_{ts}.json")

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
