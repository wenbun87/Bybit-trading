#!/usr/bin/env python3
"""
Bybit Momentum Auto-Trader

Runs the momentum scanner on a schedule, opens long positions on high-scoring
coins with no stop loss (ride-or-die strategy based on backtest results).

  Entry:  Momentum score 60+ → market buy perp (no stops)
  Exit:   Manual — close when you want

DRY-RUN MODE (default):
  Paper trades tracked with live P&L updates every scan cycle.
  Full P&L summary on Ctrl+C.

LIVE MODE:
  Real orders placed. Open position P&L shown every scan cycle.

SAFETY FEATURES:
  - Starts in DRY-RUN mode by default (no real trades until you pass --live)
  - Max trades per cycle and per day
  - 6h re-entry cooldown per symbol
  - Max total exposure cap
  - All trades logged to CSV

REQUIRES:
  export BYBIT_API_KEY="your_key"
  export BYBIT_API_SECRET="your_secret"

Usage:
    python3 auto_trader.py                    # dry-run with paper P&L tracking
    python3 auto_trader.py --live             # REAL TRADES on mainnet (no stops)
    python3 auto_trader.py --live --amount 250  # $250 per trade
    python3 auto_trader.py --min-score 70     # trigger on score 70+
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

# Import the scanner
from momentum_scanner import run_scan, fetch_all_linear_tickers, MAINNET_URL, TESTNET_URL

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

USER_AGENT = "bybit-skill/1.2.3"
RECV_WINDOW = "5000"

# Safety defaults
DEFAULT_AMOUNT_USDT = 500       # $ per trade
DEFAULT_MIN_SCORE = 60          # HIGH threshold (catch momentum early)
DEFAULT_INTERVAL_MIN = 15       # scan every 15 minutes
MAX_TRADES_PER_CYCLE = 2        # max trades per scan cycle
MAX_TRADES_PER_DAY = 6          # max trades in 24 hours
MAX_TOTAL_EXPOSURE_USDT = 3000  # stop opening if total exceeds this
DEFAULT_LEVERAGE = 10           # 10x leverage
MIN_VOLUME_24H = 5_000_000      # only trade coins with >$5M 24h volume
RE_ENTRY_COOLDOWN_HOURS = 6     # allow re-entry on same symbol after this cooldown

TRADE_LOG_FILE = "trade_log.csv"

# ──────────────────────────────────────────────
# Authenticated API client
# ──────────────────────────────────────────────

def get_credentials():
    """Load API credentials from environment variables."""
    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        print("\n  ERROR: BYBIT_API_KEY and BYBIT_API_SECRET must be set as environment variables.")
        print("  Add these to your ~/.zshrc or ~/.bashrc:")
        print('    export BYBIT_API_KEY="your_key"')
        print('    export BYBIT_API_SECRET="your_secret"')
        sys.exit(1)
    return api_key, api_secret


def sign_request(api_key: str, api_secret: str, timestamp: str, params_str: str) -> str:
    """Generate HMAC-SHA256 signature for Bybit API."""
    sign_str = f"{timestamp}{api_key}{RECV_WINDOW}{params_str}"
    return hmac.new(
        api_secret.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def api_request(base_url: str, method: str, path: str, api_key: str,
                api_secret: str, params: dict | None = None) -> dict:
    """Authenticated API request to Bybit."""
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
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST"
        )
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
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"  HTTP {e.code}: {error_body[:200]}")
        return {"retCode": -1, "retMsg": str(e)}
    except urllib.error.URLError as e:
        print(f"  Network error: {e}")
        return {"retCode": -1, "retMsg": str(e)}


# ──────────────────────────────────────────────
# Trading functions
# ──────────────────────────────────────────────

def check_balance(base_url: str, api_key: str, api_secret: str) -> float | None:
    """Get available USDT balance."""
    data = api_request(base_url, "GET", "/v5/account/wallet-balance",
                       api_key, api_secret, {"accountType": "UNIFIED"})
    if data.get("retCode") != 0:
        print(f"  Balance check failed: {data.get('retMsg')}")
        return None

    coins = data.get("result", {}).get("list", [])
    for account in coins:
        for coin in account.get("coin", []):
            if coin.get("coin") == "USDT":
                val = coin.get("availableToWithdraw", "0")
                return float(val) if val else 0.0
    return 0.0


def get_open_positions(base_url: str, api_key: str, api_secret: str) -> list[dict]:
    """Get all open linear positions."""
    data = api_request(base_url, "GET", "/v5/position/list",
                       api_key, api_secret, {"category": "linear", "settleCoin": "USDT"})
    if data.get("retCode") != 0:
        return []
    positions = data.get("result", {}).get("list", [])
    return [p for p in positions if float(p.get("size", 0)) > 0]


def get_instrument_info(base_url: str, symbol: str) -> dict | None:
    """Get instrument precision info (public endpoint)."""
    url = f"{base_url}/v5/market/instruments-info?category=linear&symbol={symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            items = data.get("result", {}).get("list", [])
            return items[0] if items else None
    except Exception:
        return None


def calculate_qty(amount_usdt: float, price: float, instrument: dict) -> str | None:
    """Calculate order quantity respecting instrument precision."""
    if price <= 0:
        return None

    lot_filter = instrument.get("lotSizeFilter", {})
    min_qty = float(lot_filter.get("minOrderQty", "0.001"))
    qty_step = float(lot_filter.get("qtyStep", "0.001"))

    raw_qty = amount_usdt / price
    if raw_qty < min_qty:
        return None

    # Round down to nearest step
    steps = int(raw_qty / qty_step)
    qty = steps * qty_step

    if qty < min_qty:
        return None

    # Format without trailing zeros
    if qty_step >= 1:
        return str(int(qty))
    decimals = len(str(qty_step).rstrip("0").split(".")[-1])
    return f"{qty:.{decimals}f}"


def set_leverage(base_url: str, api_key: str, api_secret: str,
                 symbol: str, leverage: int) -> bool:
    """Set leverage for a symbol. Returns True on success."""
    data = api_request(base_url, "POST", "/v5/position/set-leverage",
                       api_key, api_secret, {
                           "category": "linear",
                           "symbol": symbol,
                           "buyLeverage": str(leverage),
                           "sellLeverage": str(leverage),
                       })
    ret = data.get("retCode", -1)
    # 110043 = leverage already set to this value (not an error)
    return ret == 0 or ret == 110043


def place_market_order(base_url: str, api_key: str, api_secret: str,
                       symbol: str, qty: str, side: str = "Buy",
                       stop_loss: float | None = None) -> dict:
    """Place a market order on linear perpetuals with optional stop loss."""
    order_link_id = f"momentum_{symbol}_{int(time.time())}"
    params = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty,
        "orderLinkId": order_link_id,
        "positionIdx": 0,  # one-way mode
    }
    if stop_loss is not None:
        params["stopLoss"] = str(stop_loss)
    return api_request(base_url, "POST", "/v5/order/create",
                       api_key, api_secret, params)


# ──────────────────────────────────────────────
# Trade logging
# ──────────────────────────────────────────────

def init_trade_log():
    """Create trade log CSV if it doesn't exist."""
    if not Path(TRADE_LOG_FILE).exists():
        with open(TRADE_LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "symbol", "side", "qty", "price",
                "amount_usdt", "leverage", "momentum_score",
                "vol_mult", "oi_change", "status", "order_id", "mode",
            ])


def log_trade(symbol: str, side: str, qty: str, price: float,
              amount_usdt: float, leverage: int, score: float,
              signals: dict, status: str, order_id: str, mode: str):
    """Append trade to CSV log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    vol_mult = signals.get("volume_anomaly", {}).get("multiplier_1h", 0)
    oi_change = signals.get("oi_surge", {}).get("oi_change_pct", 0)
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            now, symbol, side, qty, price, amount_usdt,
            leverage, score, vol_mult, oi_change, status, order_id, mode,
        ])


# ──────────────────────────────────────────────
# Paper trading (non-live P&L tracking)
# ──────────────────────────────────────────────

class MomentumPaperTrader:
    """Tracks hypothetical momentum trades during dry-run / scan-only mode."""

    def __init__(self, amount_usdt, leverage):
        self.amount = amount_usdt
        self.leverage = leverage
        self.positions = {}       # symbol -> position dict
        self.closed_trades = []   # completed trades
        self.start_time = time.time()

    def enter(self, symbol, price, score, signals):
        if symbol in self.positions:
            return
        qty = (self.amount * self.leverage) / price
        self.positions[symbol] = {
            "entry_price": price,
            "qty": qty,
            "entry_time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "score": score,
        }
        print(f"  [PAPER] LONG {symbol} @ {price:,.6g} | "
              f"Score {score:.0f} | ${self.amount} x{self.leverage}")

    def update_prices(self, base_url):
        """Fetch latest prices for all open paper positions."""
        if not self.positions:
            return
        tickers = fetch_all_linear_tickers(base_url)
        price_map = {}
        for t in tickers:
            try:
                price_map[t["symbol"]] = float(t["lastPrice"])
            except (KeyError, ValueError, TypeError):
                continue
        for symbol, pos in self.positions.items():
            price = price_map.get(symbol)
            if price:
                pos["current_price"] = price

    def display_positions(self):
        if not self.positions:
            return
        print(f"\n  {'─'*90}")
        print(f"  PAPER POSITIONS ({len(self.positions)} open)")
        print(f"  {'─'*90}")
        print(f"  {'Symbol':<14} {'Score':>6} {'Entry':>12} {'Current':>12}"
              f"  {'P&L%':>8}  {'P&L$':>10}")
        print(f"  {'─'*90}")

        total_pnl = 0
        for symbol, pos in sorted(self.positions.items()):
            price = pos.get("current_price", pos["entry_price"])
            entry = pos["entry_price"]
            pnl_pct = (price - entry) / entry * 100
            pnl_usd = pnl_pct / 100 * self.amount * self.leverage
            total_pnl += pnl_usd

            print(f"  {symbol:<14} {pos['score']:>6.0f} {entry:>12,.6g} {price:>12,.6g}"
                  f"  {pnl_pct:>+7.1f}%  ${pnl_usd:>+9,.2f}")

        print(f"  {'─'*90}")
        print(f"  {'Total unrealized P&L:':>60}  ${total_pnl:>+9,.2f}")
        print()

    def display_periodic_summary(self):
        unrealized = 0
        for pos in self.positions.values():
            price = pos.get("current_price", pos["entry_price"])
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
            unrealized += pnl_pct / 100 * self.amount * self.leverage

        elapsed = time.time() - self.start_time
        mins = int(elapsed / 60)

        print(f"\n  {'='*80}")
        print(f"  PAPER P&L UPDATE ({mins}m elapsed)")
        print(f"  {'─'*80}")
        print(f"    Open:      {len(self.positions)} position(s) | Unrealized: ${unrealized:+,.2f}")
        print(f"  {'='*80}")

    def display_summary(self):
        elapsed = time.time() - self.start_time
        hours = elapsed / 3600
        mins = (elapsed % 3600) / 60

        print(f"\n{'='*90}")
        print(f"  PAPER TRADING SUMMARY")
        print(f"  Session: {int(hours)}h {int(mins)}m | "
              f"${self.amount} per trade @ {self.leverage}x leverage | No stops")
        print(f"{'='*90}")

        if not self.positions:
            print(f"\n  No paper trades were opened during this session.")
            print(f"{'='*90}\n")
            return

        print(f"\n  ALL POSITIONS ({len(self.positions)}):")
        print(f"  {'─'*85}")
        print(f"  {'Symbol':<14} {'Score':>6} {'Entry':>12} {'Current':>12}"
              f"  {'P&L%':>8}  {'P&L$':>10}  {'Entered'}")
        print(f"  {'─'*85}")

        total_pnl = 0
        wins = 0
        losses = 0
        for symbol, pos in sorted(self.positions.items(), key=lambda x: x[1].get("current_price", x[1]["entry_price"]) / x[1]["entry_price"] - 1, reverse=True):
            price = pos.get("current_price", pos["entry_price"])
            entry = pos["entry_price"]
            pnl_pct = (price - entry) / entry * 100
            pnl_usd = pnl_pct / 100 * self.amount * self.leverage
            total_pnl += pnl_usd
            if pnl_usd >= 0:
                wins += 1
            else:
                losses += 1

            print(f"  {symbol:<14} {pos['score']:>6.0f} {entry:>12,.6g} {price:>12,.6g}"
                  f"  {pnl_pct:>+7.1f}%  ${pnl_usd:>+9,.2f}  {pos['entry_time']}")

        print(f"  {'─'*85}")
        total_trades = len(self.positions)
        print(f"\n  RESULTS:")
        print(f"    Total trades:  {total_trades}")
        print(f"    Winning:       {wins} ({wins/total_trades*100:.0f}%)")
        print(f"    Losing:        {losses} ({losses/total_trades*100:.0f}%)")
        print(f"\n    TOTAL P&L:     ${total_pnl:+,.2f}")
        print(f"{'='*90}\n")


# ──────────────────────────────────────────────
# Live P&L display
# ──────────────────────────────────────────────

def display_live_pnl(base_url, api_key, api_secret):
    """Show real position P&L summary for live mode."""
    positions = get_open_positions(base_url, api_key, api_secret)
    if not positions:
        print(f"\n  No open positions.")
        return

    total_pnl = 0.0
    print(f"\n  {'='*90}")
    print(f"  LIVE POSITIONS — {len(positions)} open")
    print(f"  {'─'*90}")
    print(f"  {'Symbol':<14} {'Side':<6} {'Lev':>4} {'Entry':>12} {'Mark':>12}"
          f"  {'P&L%':>8}  {'P&L$':>10}  {'Value':>10}")
    print(f"  {'─'*90}")

    for pos in positions:
        symbol = pos.get("symbol", "")
        side = pos.get("side", "")
        entry_price = float(pos.get("avgPrice", "0") or "0")
        mark_price = float(pos.get("markPrice", "0") or "0")
        leverage = pos.get("leverage", "?")
        unrealised_pnl = float(pos.get("unrealisedPnl", "0") or "0")
        position_value = float(pos.get("positionValue", "0") or "0")

        if entry_price <= 0 or mark_price <= 0:
            continue

        total_pnl += unrealised_pnl
        if side == "Buy":
            profit_pct = (mark_price - entry_price) / entry_price * 100
        else:
            profit_pct = (entry_price - mark_price) / entry_price * 100

        print(f"  {symbol:<14} {side:<6} {leverage:>4}x {entry_price:>12,.6g} {mark_price:>12,.6g}"
              f"  {profit_pct:>+7.1f}%  ${unrealised_pnl:>+9,.2f}  ${position_value:>9,.2f}")

    print(f"  {'─'*90}")
    print(f"  {'TOTAL P&L:':>60}  ${total_pnl:>+9,.2f}")
    print(f"  {'='*90}")


# ──────────────────────────────────────────────
# Session state
# ──────────────────────────────────────────────

class TradingSession:
    """Track session state for safety limits."""

    def __init__(self, max_per_cycle: int, max_per_day: int, max_exposure: float):
        self.max_per_cycle = max_per_cycle
        self.max_per_day = max_per_day
        self.max_exposure = max_exposure
        # Map symbol -> unix_ts of last trade (enables ARIA-style re-entry after cooldown)
        self.traded_symbols: dict[str, float] = {}
        self.trades_today = 0
        self.today_date = datetime.now(timezone.utc).date()
        self.total_exposure = 0.0

    def can_trade(self, symbol: str) -> tuple[bool, str]:
        """Check if a trade is allowed. Returns (allowed, reason)."""
        # Reset daily counter if new day
        current_date = datetime.now(timezone.utc).date()
        if current_date != self.today_date:
            self.trades_today = 0
            self.today_date = current_date

        if symbol in self.traded_symbols:
            elapsed_hrs = (time.time() - self.traded_symbols[symbol]) / 3600
            if elapsed_hrs < RE_ENTRY_COOLDOWN_HOURS:
                remaining = RE_ENTRY_COOLDOWN_HOURS - elapsed_hrs
                return False, f"traded {symbol} {elapsed_hrs:.1f}h ago (cooldown {remaining:.1f}h left)"
        if self.trades_today >= self.max_per_day:
            return False, f"daily limit reached ({self.max_per_day} trades)"
        if self.total_exposure >= self.max_exposure:
            return False, f"exposure cap reached (${self.max_exposure:,.0f})"
        return True, ""

    def record_trade(self, symbol: str, amount: float):
        self.traded_symbols[symbol] = time.time()
        self.trades_today += 1
        self.total_exposure += amount


# ──────────────────────────────────────────────
# Main auto-trader loop
# ──────────────────────────────────────────────

def run_auto_trader(args):
    base_url = TESTNET_URL if args.testnet else MAINNET_URL
    env_label = "TESTNET" if args.testnet else "MAINNET"
    mode = "LIVE" if args.live else "DRY-RUN"

    api_key, api_secret = get_credentials()

    session = TradingSession(
        max_per_cycle=MAX_TRADES_PER_CYCLE,
        max_per_day=MAX_TRADES_PER_DAY,
        max_exposure=MAX_TOTAL_EXPOSURE_USDT,
    )

    init_trade_log()

    paper = None if args.live else MomentumPaperTrader(args.amount, args.leverage)

    # Display config
    print(f"\n{'='*70}")
    print(f"  MOMENTUM AUTO-TRADER [{env_label}] [{mode}]")
    print(f"{'='*70}")
    print(f"  Trade amount:    ${args.amount} USDT per trade")
    print(f"  Leverage:        {args.leverage}x")
    print(f"  Strategy:        No stops (ride or die)")
    print(f"  Min score:       {args.min_score} (trigger threshold)")
    print(f"  Scan interval:   every {args.interval} minutes")
    print(f"  Max per cycle:   {MAX_TRADES_PER_CYCLE} trades")
    print(f"  Max per day:     {MAX_TRADES_PER_DAY} trades")
    print(f"  Max exposure:    ${MAX_TOTAL_EXPOSURE_USDT:,}")
    print(f"  Min 24h volume:  ${MIN_VOLUME_24H/1e6:.0f}M (filters micro-caps)")
    print(f"  Re-entry after:  {RE_ENTRY_COOLDOWN_HOURS}h cooldown")
    print(f"  Trade log:       {TRADE_LOG_FILE}")

    if not args.live:
        print(f"\n  >>> DRY-RUN MODE — paper trades tracked with P&L <<<")
        print(f"  >>> Add --live flag to enable real trading <<<")
        print(f"  >>> P&L summary shown after each scan. Ctrl+C for final summary <<<")
    else:
        print(f"\n  >>> LIVE MODE — REAL ORDERS WILL BE PLACED (no stops) <<<")
        print(f"  >>> Trading ${args.amount} per signal on {env_label} <<<")

    # Verify connection
    print(f"\n  Verifying connection...")
    balance = check_balance(base_url, api_key, api_secret)
    if balance is None:
        print("  Failed to connect. Check your API credentials.")
        sys.exit(1)
    print(f"  Connected. Available balance: ${balance:,.2f} USDT")

    positions = get_open_positions(base_url, api_key, api_secret)
    print(f"  Open positions: {len(positions)}")
    print(f"{'='*70}\n")

    # Main loop
    cycle = 0
    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        print(f"\n--- Cycle {cycle} | {now} | {mode} ---\n")

        # Run momentum scan
        results = run_scan(base_url, top_n=20, min_score=args.min_score)

        if not results:
            print(f"\n  No coins above score {args.min_score}. Waiting...\n")
        else:
            # Filter to extreme signals only
            extreme = [r for r in results if r["momentum_score"] >= args.min_score]
            print(f"\n  {len(extreme)} coin(s) above threshold ({args.min_score}):\n")

            trades_this_cycle = 0
            for r in extreme:
                if trades_this_cycle >= MAX_TRADES_PER_CYCLE:
                    print(f"  Max trades per cycle reached ({MAX_TRADES_PER_CYCLE}). Skipping rest.")
                    break

                symbol = r["symbol"]
                score = r["momentum_score"]
                price = r["lastPrice"]
                signals = r["signals"]

                turnover = r.get("turnover24h", 0)
                print(f"  >> {symbol} | Score: {score} | Price: ${price:,.6g} | 24h: {r['change24h']:+.1f}% | Vol: ${turnover/1e6:,.1f}M")
                print(f"     Vol: {signals['volume_anomaly']['detail']}")
                print(f"     OI:  {signals['oi_surge']['detail']}")
                sqz = signals.get("squeeze_setup", {})
                dist = signals.get("distribution_risk", {})
                if sqz.get("score", 0) > 0:
                    print(f"     Squeeze setup: {sqz['detail']} (score {sqz['score']})")
                if dist.get("penalty", 0) > 0:
                    print(f"     Dist penalty:  -{dist['penalty']} pts | {dist['detail']}")

                # Volume filter: skip coins with < $20M 24h volume
                if turnover < MIN_VOLUME_24H:
                    print(f"     SKIP: 24h volume ${turnover/1e6:,.1f}M < ${MIN_VOLUME_24H/1e6:.0f}M minimum")
                    continue

                # Safety checks
                can_trade, reason = session.can_trade(symbol)
                if not can_trade:
                    print(f"     SKIP: {reason}")
                    continue

                # Get instrument info for qty precision
                instrument = get_instrument_info(base_url, symbol)
                if not instrument:
                    print(f"     SKIP: could not fetch instrument info")
                    continue

                qty = calculate_qty(args.amount, price, instrument)
                if not qty:
                    print(f"     SKIP: qty too small for ${args.amount} at ${price}")
                    continue

                est_value = float(qty) * price
                print(f"     Order: BUY {qty} {symbol} (~${est_value:,.2f}) @ {args.leverage}x leverage (no stops)")

                if not args.live:
                    # Dry run — track as paper trade
                    print(f"     [DRY-RUN] Would place order (no stops)")
                    log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                              score, signals, "dry-run", "N/A", "dry-run")
                    if paper:
                        paper.enter(symbol, price, score, signals)
                    session.record_trade(symbol, est_value)
                    trades_this_cycle += 1
                else:
                    # LIVE: set leverage then place order (no stop loss)
                    print(f"     Setting leverage to {args.leverage}x...", end=" ")
                    lev_ok = set_leverage(base_url, api_key, api_secret, symbol, args.leverage)
                    print("OK" if lev_ok else "WARN (may already be set)")

                    time.sleep(0.3)
                    print(f"     Placing market order (no stops)...", end=" ")
                    result = place_market_order(base_url, api_key, api_secret, symbol, qty)
                    ret_code = result.get("retCode", -1)
                    order_id = result.get("result", {}).get("orderId", "N/A")

                    if ret_code == 0:
                        print(f"FILLED (orderId: {order_id})")
                        log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                  score, signals, "filled", order_id, "live")
                        session.record_trade(symbol, est_value)
                        trades_this_cycle += 1
                    elif ret_code == 10001 and "position idx" in result.get("retMsg", "").lower():
                        print(f"hedge mode detected, retrying...", end=" ")
                        time.sleep(0.3)
                        order_link_id = f"momentum_{symbol}_{int(time.time())}"
                        hedge_params = {
                            "category": "linear", "symbol": symbol,
                            "side": "Buy", "orderType": "Market", "qty": qty,
                            "orderLinkId": order_link_id, "positionIdx": 1,
                        }
                        result2 = api_request(base_url, "POST", "/v5/order/create",
                                              api_key, api_secret, hedge_params)
                        if result2.get("retCode") == 0:
                            oid = result2.get("result", {}).get("orderId", "N/A")
                            print(f"FILLED (orderId: {oid})")
                            log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                      score, signals, "filled", oid, "live")
                            session.record_trade(symbol, est_value)
                            trades_this_cycle += 1
                        else:
                            print(f"FAILED: {result2.get('retMsg')}")
                            log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                      score, signals, "failed", "N/A", "live")
                    else:
                        print(f"FAILED: {result.get('retMsg')}")
                        log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                  score, signals, "failed", "N/A", "live")

                print()

        # P&L display
        if paper:
            paper.update_prices(base_url)
            paper.display_positions()
            paper.display_periodic_summary()
        elif args.live:
            display_live_pnl(base_url, api_key, api_secret)

        # Session summary
        print(f"\n  Session: {session.trades_today} trades today | "
              f"{len(session.traded_symbols)} unique coins | "
              f"${session.total_exposure:,.0f} exposure")

        print(f"\n  Next scan in {args.interval} min... (Ctrl+C to stop)")
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            if paper:
                paper.display_summary()
            print(f"\n{'='*70}")
            print(f"  AUTO-TRADER STOPPED")
            print(f"  Trades this session: {session.trades_today}")
            print(f"  Coins traded: {', '.join(session.traded_symbols) or 'none'}")
            print(f"  Total exposure: ${session.total_exposure:,.0f}")
            print(f"  Log file: {TRADE_LOG_FILE}")
            print(f"{'='*70}\n")
            break


def main():
    parser = argparse.ArgumentParser(
        description="Bybit Momentum Auto-Trader — automatically trade extreme momentum coins"
    )
    parser.add_argument("--live", action="store_true",
                        help="Enable LIVE trading (default is dry-run)")
    parser.add_argument("--testnet", action="store_true",
                        help="Use testnet instead of mainnet")
    parser.add_argument("--amount", type=float, default=DEFAULT_AMOUNT_USDT,
                        help=f"USDT amount per trade (default: ${DEFAULT_AMOUNT_USDT})")
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE,
                        help=f"Leverage multiplier (default: {DEFAULT_LEVERAGE}x)")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                        help=f"Minimum momentum score to trigger (default: {DEFAULT_MIN_SCORE})")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN,
                        help=f"Scan interval in minutes (default: {DEFAULT_INTERVAL_MIN})")
    args = parser.parse_args()

    if args.live and not args.testnet:
        print(f"\n  WARNING: You are about to run LIVE auto-trading on MAINNET.")
        print(f"  This will place REAL orders with REAL money.")
        print(f"  Amount: ${args.amount} per trade | Leverage: {args.leverage}x")
        print(f"  Max daily: {MAX_TRADES_PER_DAY} trades (${MAX_TRADES_PER_DAY * args.amount:,.0f})")
        confirm = input("\n  Type CONFIRM to proceed: ").strip()
        if confirm.upper() != "CONFIRM":
            print("  Cancelled.")
            sys.exit(0)

    run_auto_trader(args)


if __name__ == "__main__":
    main()
