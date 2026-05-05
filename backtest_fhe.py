#!/usr/bin/env python3
"""
Backtest: Would the accumulation scanner have caught FHE 2 days ago?

Fetches historical data from Bybit API and simulates the scanner's
analysis as of 2 days ago (before the pump).

Usage: python3 backtest_fhe.py
"""
import sys
import os
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from momentum_scanner import (
    api_get,
    fetch_all_linear_tickers,
    analyze_accumulation,
    analyze_volume_anomaly,
    analyze_price_acceleration,
    analyze_oi_surge,
    analyze_funding_shift,
    analyze_streak,
    analyze_squeeze_setup,
    analyze_pre_squeeze_setup,
    analyze_distribution_risk,
    analyze_crime_pump_risk,
    compute_momentum_score,
    fetch_supply_data,
    MAINNET_URL,
    MIN_TURNOVER_24H,
)

SYMBOL = "FHEUSDT"
BASE_URL = MAINNET_URL

# "2 days ago" = the start of the day 2 days before today
DAYS_AGO = 2
target_time = datetime.now(timezone.utc) - timedelta(days=DAYS_AGO)
target_ts_ms = int(target_time.timestamp() * 1000)
target_label = target_time.strftime("%Y-%m-%d %H:%M UTC")


def fetch_klines_before(symbol, interval, limit, end_ms):
    """Fetch klines ending BEFORE a given timestamp."""
    data = api_get(BASE_URL, "/v5/market/kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
        "end": str(end_ms),
    })
    klines = data.get("result", {}).get("list", [])
    return list(reversed(klines))  # oldest first


def fetch_oi_before(symbol, end_ms):
    """Fetch OI data ending before a given timestamp."""
    data = api_get(BASE_URL, "/v5/market/open-interest", {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": "5min",
        "limit": "48",
        "endTime": str(end_ms),
    })
    return data.get("result", {}).get("list", [])


def fetch_funding_before(symbol, end_ms):
    """Fetch funding rate history before a given timestamp."""
    data = api_get(BASE_URL, "/v5/market/funding/history", {
        "category": "linear",
        "symbol": symbol,
        "limit": "10",
        "endTime": str(end_ms),
    })
    return data.get("result", {}).get("list", [])


def fetch_ls_ratio_before(symbol, end_ms):
    """Fetch long/short ratio."""
    data = api_get(BASE_URL, "/v5/market/account-ratio", {
        "category": "linear",
        "symbol": symbol,
        "period": "5min",
        "limit": "12",
    })
    return data.get("result", {}).get("list", [])


def main():
    print(f"\n{'='*70}")
    print(f"  BACKTEST: Would the scanner have caught {SYMBOL}?")
    print(f"  Simulating scanner state as of: {target_label}")
    print(f"{'='*70}\n")

    # ── Step 1: Fetch historical daily klines (as of 2 days ago) ──
    print(f"  Fetching daily klines for {SYMBOL} (ending {DAYS_AGO} days ago)...")
    daily_klines = fetch_klines_before(SYMBOL, "D", 14, target_ts_ms)
    if not daily_klines:
        print(f"  ERROR: No daily kline data returned for {SYMBOL}. Is it listed on Bybit?")
        return

    print(f"  Got {len(daily_klines)} daily candles")
    print(f"\n  Daily candles (oldest → newest):")
    print(f"  {'Date':<12} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Turnover':>14}")
    print(f"  {'-'*70}")
    for k in daily_klines:
        try:
            ts = int(k[0])
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            turnover = float(k[6])
            print(f"  {dt:<12} ${o:>9.6g} ${h:>9.6g} ${l:>9.6g} ${c:>9.6g} ${turnover/1e6:>10.2f}M")
        except (ValueError, IndexError):
            pass

    # ── Step 2: What was the 24h turnover on the target day? ──
    if len(daily_klines) >= 1:
        last_candle = daily_klines[-1]
        turnover_24h = float(last_candle[6])
        price_close = float(last_candle[4])
        print(f"\n  On target day:")
        print(f"    24h turnover: ${turnover_24h/1e6:,.2f}M")
        print(f"    Close price:  ${price_close:,.6g}")
    else:
        turnover_24h = 0
        price_close = 0

    # ── Step 3: Would FHE have been in Pool D? ──
    print(f"\n  Pool D filter check:")
    print(f"    MIN_TURNOVER_24H: ${MIN_TURNOVER_24H/1e6:.1f}M")
    passes_min = turnover_24h >= MIN_TURNOVER_24H
    print(f"    Passes min turnover: {'YES' if passes_min else 'NO'} (${turnover_24h/1e6:,.2f}M {'>' if passes_min else '<'} ${MIN_TURNOVER_24H/1e6:.1f}M)")
    in_pool_d_range = turnover_24h <= 5_000_000
    print(f"    In Pool D range (<=5M): {'YES' if in_pool_d_range else 'NO'} (${turnover_24h/1e6:,.2f}M)")

    # Would it rank in top 50 by abs(change)?
    if len(daily_klines) >= 2:
        prev_close = float(daily_klines[-2][4])
        if prev_close > 0:
            day_change = (price_close - prev_close) / prev_close * 100
            print(f"    24h change: {day_change:+.1f}%")
            print(f"    abs(change) for ranking: {abs(day_change):.1f}% — would rank {'HIGH' if abs(day_change) > 5 else 'MODERATE' if abs(day_change) > 1 else 'LOW'} in Pool D sort")

    # ── Step 4: Fetch remaining data for scoring ──
    print(f"\n  Fetching hourly klines, OI, funding...")
    hourly_klines = fetch_klines_before(SYMBOL, "60", 48, target_ts_ms)
    oi_data = fetch_oi_before(SYMBOL, target_ts_ms)
    funding_data = fetch_funding_before(SYMBOL, target_ts_ms)
    ls_ratio = fetch_ls_ratio_before(SYMBOL, target_ts_ms)

    print(f"    Hourly klines: {len(hourly_klines)}")
    print(f"    OI snapshots:  {len(oi_data)}")
    print(f"    Funding rates: {len(funding_data)}")

    # ── Step 5: Fetch CoinGecko supply data ──
    print(f"  Fetching CoinGecko supply data...")
    supply_data = fetch_supply_data([SYMBOL])
    supply_info = supply_data.get(SYMBOL)
    if supply_info:
        print(f"    Circ ratio: {supply_info.get('circ_ratio', 'N/A')}")
        print(f"    Market cap: ${supply_info.get('market_cap', 0)/1e6:,.1f}M")
    else:
        print(f"    No CoinGecko data found for {SYMBOL}")

    # ── Step 6: Run ALL signal analyzers (same as scanner does) ──
    print(f"\n  Running signal analysis...")
    signals = {
        "volume_anomaly": analyze_volume_anomaly(hourly_klines),
        "price_accel": analyze_price_acceleration(hourly_klines),
        "oi_surge": analyze_oi_surge(oi_data),
        "funding_shift": analyze_funding_shift(funding_data),
        "streak": analyze_streak(hourly_klines),
        "squeeze_setup": analyze_squeeze_setup(funding_data, hourly_klines, oi_data, ls_ratio),
        "pre_squeeze": analyze_pre_squeeze_setup(funding_data, hourly_klines, oi_data),
        "accumulation": analyze_accumulation(daily_klines, oi_data, funding_data,
                                            turnover_24h, supply_info),
        "distribution_risk": analyze_distribution_risk(hourly_klines, oi_data),
        "crime_pump": analyze_crime_pump_risk(hourly_klines, oi_data, funding_data,
                                              turnover_24h, daily_klines),
    }

    # ── Step 7: Display all signal scores ──
    print(f"\n  {'Signal':<22} {'Score':>6} {'Detail'}")
    print(f"  {'-'*70}")
    for name, sig in signals.items():
        score = sig.get("score", sig.get("penalty", sig.get("crime_score", 0)))
        detail = sig.get("detail", "")
        if name == "distribution_risk":
            score = -sig.get("penalty", 0)
            detail = sig.get("detail", "")
        elif name == "crime_pump":
            score = sig.get("crime_score", 0)
            blocked = sig.get("blocked", False)
            detail = f"{'BLOCKED!' if blocked else ''} {sig.get('detail', '')}".strip()
        print(f"  {name:<22} {score:>6.0f}   {detail[:60]}")

    # ── Step 8: Compute final momentum score ──
    momentum_score = compute_momentum_score(signals)
    crime_blocked = signals["crime_pump"]["blocked"]

    print(f"\n  {'='*70}")
    print(f"  FINAL MOMENTUM SCORE: {momentum_score:.1f}")
    if crime_blocked:
        print(f"  CRIME PUMP BLOCKED: YES — scanner would have SKIPPED this coin")

    # Accumulation detail
    acc = signals["accumulation"]
    print(f"\n  Accumulation signal breakdown:")
    print(f"    Raw score:  {acc['score']}")
    print(f"    Phase:      {acc['phase']}")
    print(f"    Vol ramp:   {acc['vol_ramp']:.1f}x")
    print(f"    5d range:   {acc['range_5d']:.1f}%")
    print(f"    Detail:     {acc['detail']}")

    # ── Step 9: Verdict ──
    print(f"\n  {'='*70}")
    print(f"  VERDICT:")

    if crime_blocked:
        print(f"    Would FHE have been in scan pool? YES (Pool D)")
        print(f"    Would it have been BLOCKED by crime filter? YES")
        print(f"    RESULT: MISSED (blocked as crime pump)")
    elif not passes_min:
        print(f"    Would FHE have passed min turnover filter? NO")
        print(f"    RESULT: MISSED (below ${MIN_TURNOVER_24H/1e6:.1f}M turnover)")
    elif not in_pool_d_range:
        print(f"    Would FHE have been in Pool D? NO (turnover ${turnover_24h/1e6:,.1f}M > $5M)")
        print(f"    It would be in Pool A or B (already pumping)")
        print(f"    RESULT: SEEN but as Pool A/B (watch only, not traded)")
    else:
        # Pool D entry thresholds
        composite_pass = momentum_score >= 25
        accum_pass = acc["score"] >= 20
        entered = composite_pass and accum_pass

        print(f"    Pool D candidate? YES (${turnover_24h/1e6:,.2f}M turnover)")
        print(f"    Composite score {momentum_score:.1f} >= 25? {'YES' if composite_pass else 'NO'}")
        print(f"    Accumulation score {acc['score']} >= 20? {'YES' if accum_pass else 'NO'}")
        if entered:
            print(f"    RESULT: CAUGHT! Would have entered a paper trade.")
        else:
            print(f"    RESULT: MISSED (below entry thresholds)")

    # Also check what the CURRENT data looks like
    print(f"\n  {'='*70}")
    print(f"  CURRENT STATE (now, for reference):")
    print(f"  Fetching current data...")
    current_tickers = fetch_all_linear_tickers(BASE_URL)
    fhe_now = None
    for t in current_tickers:
        if t["symbol"] == SYMBOL:
            fhe_now = t
            break
    if fhe_now:
        now_turnover = float(fhe_now.get("turnover24h", 0))
        now_price = float(fhe_now.get("lastPrice", 0))
        now_change = float(fhe_now.get("price24hPcnt", 0)) * 100
        print(f"    Price:    ${now_price:,.6g}")
        print(f"    24h chg:  {now_change:+.1f}%")
        print(f"    Turnover: ${now_turnover/1e6:,.1f}M")

        if price_close > 0 and now_price > 0:
            total_move = (now_price - price_close) / price_close * 100
            print(f"    Move since scanner day: {total_move:+.1f}%")
    else:
        print(f"    {SYMBOL} not found in current tickers")

    print()


if __name__ == "__main__":
    main()
