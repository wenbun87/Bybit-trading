#!/usr/bin/env python3
"""
Bybit Spot Top 20 Gainers by 24h Volume — with RSI & Funding Rates
No authentication required (all public endpoints).

Usage:
    python3 top_gainers.py
    python3 top_gainers.py --testnet     # use testnet
    python3 top_gainers.py --top 10      # show top N (default 20)
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
import urllib.error

# ---------- config ----------

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
USER_AGENT = "bybit-skill/1.2.3"
RSI_PERIOD = 14
KLINE_INTERVAL = "60"  # 1-hour candles
KLINE_LIMIT = 100      # enough for RSI calculation
MIN_CALL_INTERVAL = 0.12  # 120ms between GET calls (rate limit safety)

_last_call_ts = 0.0


def api_get(base_url: str, path: str, params: dict | None = None) -> dict:
    """GET request to Bybit public API with rate-limit pacing."""
    global _last_call_ts
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
            data = json.loads(resp.read())
            if data.get("retCode") != 0:
                print(f"  API warning: {path} → retCode={data['retCode']} retMsg={data.get('retMsg')}")
            return data
    except urllib.error.URLError as e:
        print(f"  Network error fetching {path}: {e}")
        return {"retCode": -1, "result": {}}


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Compute RSI from a list of closing prices (oldest first)."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def fetch_spot_tickers(base_url: str) -> list[dict]:
    """Fetch all spot tickers and return as list."""
    data = api_get(base_url, "/v5/market/tickers", {"category": "spot"})
    return data.get("result", {}).get("list", [])


def fetch_rsi(base_url: str, symbol: str) -> float | None:
    """Fetch 1h klines and compute 14-period RSI."""
    data = api_get(base_url, "/v5/market/kline", {
        "category": "spot",
        "symbol": symbol,
        "interval": KLINE_INTERVAL,
        "limit": str(KLINE_LIMIT),
    })
    klines = data.get("result", {}).get("list", [])
    if not klines:
        return None
    # Bybit returns newest first — reverse to oldest first
    # Each kline: [startTime, open, high, low, close, volume, turnover]
    closes = [float(k[4]) for k in reversed(klines)]
    return compute_rsi(closes, RSI_PERIOD)


def fetch_funding_rate(base_url: str, symbol: str) -> dict | None:
    """Fetch the most recent funding rate for a linear perpetual."""
    data = api_get(base_url, "/v5/market/funding/history", {
        "category": "linear",
        "symbol": symbol,
        "limit": "1",
    })
    items = data.get("result", {}).get("list", [])
    return items[0] if items else None


def main():
    parser = argparse.ArgumentParser(description="Bybit Spot Top Gainers with RSI & Funding")
    parser.add_argument("--testnet", action="store_true", help="Use testnet instead of mainnet")
    parser.add_argument("--top", type=int, default=20, help="Number of top gainers to show (default: 20)")
    args = parser.parse_args()

    base_url = TESTNET_URL if args.testnet else MAINNET_URL
    env_label = "TESTNET" if args.testnet else "MAINNET"

    print(f"\n[{env_label}] Fetching spot tickers...")
    tickers = fetch_spot_tickers(base_url)
    if not tickers:
        print("Failed to fetch tickers. Check your network connection.")
        return

    # Filter: positive 24h change, non-zero volume, USDT pairs preferred
    gainers = []
    for t in tickers:
        try:
            change_pct = float(t.get("price24hPcnt", 0)) * 100
            volume_24h = float(t.get("turnover24h", 0))  # turnover = volume in quote currency (USDT)
            last_price = float(t.get("lastPrice", 0))
        except (ValueError, TypeError):
            continue
        if change_pct > 0 and volume_24h > 0 and last_price > 0:
            gainers.append({
                "symbol": t["symbol"],
                "lastPrice": last_price,
                "change24h": change_pct,
                "volume24h": volume_24h,
                "highPrice24h": t.get("highPrice24h", "N/A"),
                "lowPrice24h": t.get("lowPrice24h", "N/A"),
            })

    # Sort by 24h volume (descending) and take top N
    gainers.sort(key=lambda x: x["volume24h"], reverse=True)
    top = gainers[: args.top]

    if not top:
        print("No gainers found.")
        return

    print(f"Found {len(gainers)} gainers. Fetching RSI & funding for top {len(top)}...\n")

    # Fetch RSI and funding rate for each top gainer
    results = []
    for i, g in enumerate(top):
        symbol = g["symbol"]
        print(f"  [{i+1}/{len(top)}] {symbol}...", end=" ", flush=True)

        rsi = fetch_rsi(base_url, symbol)

        # Try fetching funding rate (linear perp — usually same symbol name)
        funding = fetch_funding_rate(base_url, symbol)
        funding_rate = None
        if funding:
            try:
                funding_rate = float(funding.get("fundingRate", 0)) * 100
            except (ValueError, TypeError):
                pass

        results.append({
            **g,
            "rsi": rsi,
            "fundingRate": funding_rate,
        })
        print("done")

    # Display results
    print(f"\n{'='*110}")
    print(f"[{env_label}] Top {len(results)} Spot Gainers by 24h Volume — with RSI(14, 1h) & Funding Rate")
    print(f"{'='*110}")
    print(
        f"{'#':>3}  {'Symbol':<14} {'Price':>14} {'24h Chg%':>10} {'24h Volume (USDT)':>20}"
        f"  {'RSI(14)':>8}  {'Funding%':>10}  {'Signal':<12}"
    )
    print("-" * 110)

    for i, r in enumerate(results, 1):
        rsi_str = f"{r['rsi']:.1f}" if r['rsi'] is not None else "N/A"
        fund_str = f"{r['fundingRate']:.4f}%" if r['fundingRate'] is not None else "N/A"

        # Simple signal based on RSI
        if r['rsi'] is not None:
            if r['rsi'] >= 70:
                signal = "Overbought"
            elif r['rsi'] <= 30:
                signal = "Oversold"
            else:
                signal = "Neutral"
        else:
            signal = "—"

        vol_str = f"${r['volume24h']:,.0f}"

        print(
            f"{i:>3}  {r['symbol']:<14} {r['lastPrice']:>14,.6g} {r['change24h']:>+9.2f}% {vol_str:>20}"
            f"  {rsi_str:>8}  {fund_str:>10}  {signal:<12}"
        )

    print("-" * 110)
    print(f"\nRSI > 70: Overbought (potential reversal down)")
    print(f"RSI < 30: Oversold (potential reversal up)")
    print(f"Funding > 0: Longs pay shorts | Funding < 0: Shorts pay longs")
    print(f"Funding Rate = N/A means no linear perpetual contract exists for that pair\n")

    # Save raw data as JSON
    output_file = "top_gainers_data.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Raw data saved to {output_file}")


if __name__ == "__main__":
    main()
