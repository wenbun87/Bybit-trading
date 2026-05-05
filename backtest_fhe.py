#!/usr/bin/env python3
"""
Backtest: When would the accumulation scanner have caught FHE?

Tests multiple days (3-12 days ago) to find the earliest catch window.
The pump started May 2-3, so the ideal catch was during accumulation
phase (April 24-May 1).

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
TEST_DAYS = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]


def fetch_klines_before(symbol, interval, limit, end_ms):
    data = api_get(BASE_URL, "/v5/market/kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
        "end": str(end_ms),
    })
    klines = data.get("result", {}).get("list", [])
    return list(reversed(klines))


def fetch_oi_before(symbol, end_ms):
    data = api_get(BASE_URL, "/v5/market/open-interest", {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": "5min",
        "limit": "48",
        "endTime": str(end_ms),
    })
    return data.get("result", {}).get("list", [])


def fetch_funding_before(symbol, end_ms):
    data = api_get(BASE_URL, "/v5/market/funding/history", {
        "category": "linear",
        "symbol": symbol,
        "limit": "10",
        "endTime": str(end_ms),
    })
    return data.get("result", {}).get("list", [])


def fetch_ls_ratio_before(symbol, end_ms):
    data = api_get(BASE_URL, "/v5/market/account-ratio", {
        "category": "linear",
        "symbol": symbol,
        "period": "5min",
        "limit": "12",
    })
    return data.get("result", {}).get("list", [])


def run_day(days_ago, supply_info):
    """Run full signal analysis for a specific point in time."""
    target = datetime.now(timezone.utc) - timedelta(days=days_ago)
    target_ms = int(target.timestamp() * 1000)
    date_label = target.strftime("%Y-%m-%d")

    daily_klines = fetch_klines_before(SYMBOL, "D", 14, target_ms)
    if not daily_klines or len(daily_klines) < 5:
        return None

    last_candle = daily_klines[-1]
    turnover_24h = float(last_candle[6])
    price_close = float(last_candle[4])
    candle_date = datetime.fromtimestamp(int(last_candle[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    # Compute day-over-day change
    prev_close = float(daily_klines[-2][4]) if len(daily_klines) >= 2 else price_close
    day_change = (price_close - prev_close) / prev_close * 100 if prev_close > 0 else 0

    hourly_klines = fetch_klines_before(SYMBOL, "60", 48, target_ms)
    oi_data = fetch_oi_before(SYMBOL, target_ms)
    funding_data = fetch_funding_before(SYMBOL, target_ms)
    ls_ratio = fetch_ls_ratio_before(SYMBOL, target_ms)

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

    momentum_score = compute_momentum_score(signals)
    acc = signals["accumulation"]
    crime = signals["crime_pump"]

    in_pool_d = turnover_24h <= 5_000_000 and turnover_24h >= MIN_TURNOVER_24H
    composite_pass = momentum_score >= 25
    accum_pass = acc["score"] >= 20
    crime_blocked = crime["blocked"]

    if crime_blocked:
        verdict = "BLOCKED (crime)"
    elif not in_pool_d:
        if turnover_24h < MIN_TURNOVER_24H:
            verdict = "MISSED (too low vol)"
        else:
            verdict = f"Pool A/B (${turnover_24h/1e6:.1f}M)"
    elif composite_pass and accum_pass:
        verdict = "CAUGHT"
    else:
        reasons = []
        if not composite_pass:
            reasons.append(f"score {momentum_score:.0f}<25")
        if not accum_pass:
            reasons.append(f"accum {acc['score']:.0f}<20")
        verdict = f"MISSED ({', '.join(reasons)})"

    return {
        "candle_date": candle_date,
        "price": price_close,
        "turnover": turnover_24h,
        "day_change": day_change,
        "momentum_score": momentum_score,
        "accum_score": acc["score"],
        "accum_phase": acc["phase"],
        "vol_ramp": acc["vol_ramp"],
        "range_5d": acc["range_5d"],
        "accum_detail": acc["detail"],
        "crime_score": crime.get("crime_score", 0),
        "crime_blocked": crime_blocked,
        "in_pool_d": in_pool_d,
        "verdict": verdict,
        "signals": signals,
    }


def main():
    print(f"\n{'='*80}")
    print(f"  MULTI-DAY BACKTEST: When would the scanner have caught {SYMBOL}?")
    print(f"  Testing {len(TEST_DAYS)} days: {TEST_DAYS[0]}-{TEST_DAYS[-1]} days ago")
    print(f"{'='*80}")

    # Fetch supply data once (doesn't change day to day)
    print(f"\n  Fetching CoinGecko supply data for {SYMBOL}...")
    supply_data = fetch_supply_data([SYMBOL])
    supply_info = supply_data.get(SYMBOL)
    if supply_info:
        print(f"    Circ ratio: {supply_info.get('circ_ratio', 'N/A')}")
        print(f"    Market cap: ${supply_info.get('market_cap', 0)/1e6:,.1f}M")
    else:
        print(f"    No CoinGecko data found")

    results = []
    for days_ago in TEST_DAYS:
        target = datetime.now(timezone.utc) - timedelta(days=days_ago)
        print(f"\n  Testing {days_ago} days ago ({target.strftime('%Y-%m-%d')})...", end="", flush=True)
        r = run_day(days_ago, supply_info)
        if r:
            results.append(r)
            print(f" {r['verdict']}")
        else:
            print(f" no data")

    # ── Summary table ──
    print(f"\n{'='*80}")
    print(f"  RESULTS TIMELINE")
    print(f"{'='*80}")
    print(f"  {'Date':<12} {'Price':>9} {'Turnover':>10} {'Chg':>7} {'Accum':>6} {'Score':>6} {'Vol Ramp':>9} {'Verdict':<22}")
    print(f"  {'-'*85}")

    for r in results:
        print(f"  {r['candle_date']:<12} ${r['price']:>8.5g} ${r['turnover']/1e6:>7.2f}M "
              f"{r['day_change']:>+6.1f}% {r['accum_score']:>6.0f} {r['momentum_score']:>6.1f} "
              f"{r['vol_ramp']:>7.1f}x  {r['verdict']:<22}")

    # Show current price for profit calculation
    print(f"\n  Current state:")
    current_tickers = fetch_all_linear_tickers(BASE_URL)
    fhe_now = next((t for t in current_tickers if t["symbol"] == SYMBOL), None)
    if fhe_now:
        now_price = float(fhe_now.get("lastPrice", 0))
        now_turnover = float(fhe_now.get("turnover24h", 0))
        now_change = float(fhe_now.get("price24hPcnt", 0)) * 100
        print(f"    Price: ${now_price:,.6g} | Turnover: ${now_turnover/1e6:,.1f}M | 24h: {now_change:+.1f}%")

        # Show potential gains for each "CAUGHT" day
        caught_days = [r for r in results if "CAUGHT" in r["verdict"]]
        if caught_days:
            print(f"\n  Potential gains if entered on CAUGHT days:")
            for r in caught_days:
                gain = (now_price - r["price"]) / r["price"] * 100
                print(f"    {r['candle_date']}: entry ${r['price']:,.6g} → now ${now_price:,.6g} = {gain:+.1f}%")

    # Show detail for the earliest catch
    caught_days = [r for r in results if "CAUGHT" in r["verdict"]]
    if caught_days:
        earliest = caught_days[-1]  # results are newest-first
        print(f"\n  {'='*80}")
        print(f"  EARLIEST CATCH: {earliest['candle_date']}")
        print(f"  {'='*80}")
        print(f"    Price:       ${earliest['price']:,.6g}")
        print(f"    Turnover:    ${earliest['turnover']/1e6:,.2f}M")
        print(f"    Accum score: {earliest['accum_score']:.0f} ({earliest['accum_phase']})")
        print(f"    Vol ramp:    {earliest['vol_ramp']:.1f}x")
        print(f"    5d range:    {earliest['range_5d']:.1f}%")
        print(f"    Detail:      {earliest['accum_detail']}")
        print(f"\n    All signals:")
        for name, sig in earliest["signals"].items():
            score = sig.get("score", 0)
            if name == "distribution_risk":
                score = -sig.get("penalty", 0)
            elif name == "crime_pump":
                score = sig.get("crime_score", 0)
            detail = sig.get("detail", "")[:55]
            if score != 0 or name in ("accumulation", "crime_pump"):
                print(f"      {name:<22} {score:>5.0f}  {detail}")
    else:
        print(f"\n  Scanner would NOT have caught FHE on any tested day.")
        print(f"  Showing the day with best accumulation score:")
        best = max(results, key=lambda r: r["accum_score"]) if results else None
        if best:
            print(f"    {best['candle_date']}: accum={best['accum_score']:.0f}, "
                  f"score={best['momentum_score']:.1f}, ramp={best['vol_ramp']:.1f}x, "
                  f"verdict={best['verdict']}")
            print(f"    Detail: {best['accum_detail']}")

    print()


if __name__ == "__main__":
    main()
