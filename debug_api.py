#!/usr/bin/env python3
"""Quick diagnostic: what does the Bybit ticker API actually return?
Run this on your machine: python3 debug_api.py
"""
import urllib.request
import json

url = "https://api.bybit.com/v5/market/tickers?category=linear"
req = urllib.request.Request(url, headers={"User-Agent": "bybit-skill/1.2.3"})
resp = urllib.request.urlopen(req, timeout=15)
raw = resp.read()
data = json.loads(raw)

print(f"API retCode: {data.get('retCode')}")
print(f"API retMsg:  {data.get('retMsg')}")

tickers = data.get("result", {}).get("list", [])
print(f"\nTotal tickers returned: {len(tickers)}")

if not tickers:
    print("NO TICKERS RETURNED — something is wrong with the API response")
    print(f"Full response: {raw[:500]}")
    exit(1)

usdt = []
usdc = []
for t in tickers:
    sym = t["symbol"]
    if sym.endswith("USDT"):
        usdt.append(t)
    elif sym.endswith("USDC"):
        usdc.append(t)

print(f"  USDT pairs: {len(usdt)}")
print(f"  USDC pairs: {len(usdc)}")

# Parse and sort
for t in usdt:
    t["_turnover"] = float(t.get("turnover24h", 0))
    t["_volume"] = float(t.get("volume24h", 0))
usdt.sort(key=lambda x: x["_turnover"], reverse=True)

# Count by threshold
print(f"\nTurnover distribution (USDT pairs):")
for thresh in [500e6, 100e6, 50e6, 10e6, 5e6, 2e6, 1e6, 0.5e6, 0.1e6, 0]:
    count = sum(1 for t in usdt if t["_turnover"] >= thresh)
    label = f"${thresh/1e6:.0f}M" if thresh >= 1e6 else f"${thresh/1e3:.0f}K" if thresh > 0 else "$0"
    print(f"  > {label:>6} turnover: {count} pairs")

# Check for zeros
zeros = [t for t in usdt if t["_turnover"] == 0]
print(f"\n  USDT pairs with ZERO turnover: {zeros and len(zeros) or 0}")

# Top 25
print(f"\nTop 25 USDT pairs by turnover24h:")
print(f"  {'#':>3} {'Symbol':<18} {'turnover24h':>15} {'volume24h':>15} {'lastPrice':>12} {'24h%':>8}")
print(f"  {'-'*75}")
for i, t in enumerate(usdt[:25], 1):
    sym = t["symbol"]
    turnover = t["_turnover"]
    volume = t["_volume"]
    price = float(t.get("lastPrice", 0))
    change = float(t.get("price24hPcnt", 0)) * 100
    print(f"  {i:>3} {sym:<18} ${turnover/1e6:>12,.1f}M {volume:>15,.0f} ${price:>10,.6g} {change:>+7.1f}%")

# Bottom 10 (non-zero)
nonzero = [t for t in usdt if t["_turnover"] > 0]
nonzero.sort(key=lambda x: x["_turnover"])
print(f"\nBottom 10 USDT pairs (non-zero turnover):")
for t in nonzero[:10]:
    print(f"  {t['symbol']:<18} ${t['_turnover']/1e6:>8,.3f}M")

# Show specific coins from user's screenshot
print(f"\nSpecific coins (from Bybit app screenshot):")
check = ["BTCUSDT", "LABUSDT", "XRPUSDT", "TONUSDT", "DOGEUSDT", "HYPEUSDT",
         "1000PEPEUSDT", "BSBUSDT", "ETHUSDT", "SOLUSDT"]
for sym in check:
    found = [t for t in tickers if t["symbol"] == sym]
    if found:
        t = found[0]
        print(f"  {sym:<18} turnover24h={t.get('turnover24h'):>20}  volume24h={t.get('volume24h'):>20}")
    else:
        print(f"  {sym:<18} NOT FOUND in API response")

# Raw sample
print(f"\nRaw JSON for first USDT ticker:")
if usdt:
    sample = {k: v for k, v in usdt[0].items() if k != "_turnover" and k != "_volume"}
    print(json.dumps(sample, indent=2))
