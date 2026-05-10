#!/usr/bin/env python3
"""
Bybit Accumulation Auto-Trader

Scans for quiet, low-turnover coins showing early accumulation signals
(volume ramp, OI building, price coiling) and enters BEFORE the pump.

  Entry:  Pool D coins only (quiet accumulation phase) with score 60+
  Exit:   Automatic — when coin graduates to Pool A/B (crowd arrives),
          funding flips positive, OI drops, or 200%+ extension

DRY-RUN MODE (default):
  Paper trades tracked with live P&L updates every scan cycle.
  Full P&L summary on Ctrl+C.

LIVE MODE:
  Real orders placed. Exit signals checked every scan cycle.

SAFETY FEATURES:
  - Starts in DRY-RUN mode by default (no real trades until you pass --live)
  - Only enters Pool D (quiet) coins — never chases pumps
  - Automatic exit when coin hits mainstream radar
  - Max trades per cycle and per day
  - 6h re-entry cooldown per symbol
  - Max total exposure cap
  - All trades logged to CSV

REQUIRES:
  export BYBIT_API_KEY="your_key"
  export BYBIT_API_SECRET="your_secret"

Usage:
    python3 auto_trader.py                    # dry-run with paper P&L tracking
    python3 auto_trader.py --live             # REAL TRADES on mainnet
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
from momentum_scanner import (run_scan, fetch_all_linear_tickers, check_exit_signals,
                              MAINNET_URL, TESTNET_URL)
import shared_state

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

USER_AGENT = "bybit-skill/1.2.3"
RECV_WINDOW = "5000"

# Safety defaults
DEFAULT_AMOUNT_USDT = 250       # $ per trade (fixed mode, overridden by score-based)
DEFAULT_ACCOUNT_BALANCE = 500   # Account balance for score-based sizing
DEFAULT_MAX_EXPOSURE_MULT = 5   # Max total exposure = balance × this (with leverage)
DEFAULT_MIN_SCORE = 40          # ELEVATED threshold (catch accumulation earlier)
DEFAULT_INTERVAL_MIN = 15       # scan every 15 minutes
MAX_TRADES_PER_CYCLE = 2        # max trades per scan cycle
MAX_TRADES_PER_DAY = 6          # max trades in 24 hours
DEFAULT_LEVERAGE = 10           # 10x leverage
RE_ENTRY_COOLDOWN_HOURS = 6     # allow re-entry on same symbol after this cooldown
MIN_HOLD_SECONDS = 1800         # 30 min minimum hold before signal-based exits (hard exit still active)

# Score-based sizing tiers: (min_score, multiplier_of_base)
SCORE_SIZE_TIERS = [
    (80, 2.0),   # Strong signal → 2x base
    (60, 1.5),   # Good signal → 1.5x base
    (40, 1.0),   # Moderate signal → 1x base
    (25, 0.5),   # Marginal signal → 0.5x base
]


def compute_trade_size(score: float, account_balance: float, max_exposure: float,
                       current_exposure: float, leverage: int) -> float:
    """Compute trade size (margin) based on signal score and account limits."""
    base = account_balance / 10
    multiplier = 0.5
    for min_score, mult in SCORE_SIZE_TIERS:
        if score >= min_score:
            multiplier = mult
            break
    size = base * multiplier
    remaining = max(0, (max_exposure - current_exposure) / leverage)
    size = min(size, remaining)
    size = max(size, 0)
    return round(size, 2)

TRADE_LOG_FILE = "trade_log.csv"
STATE_FILE = Path(__file__).parent / "data" / "auto_trader_state.json"

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
    except (urllib.error.URLError, TimeoutError, OSError) as e:
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
    """Tracks hypothetical accumulation trades during dry-run / scan-only mode."""

    def __init__(self, amount_usdt, leverage):
        self.amount = amount_usdt
        self.leverage = leverage
        self.positions = {}       # symbol -> position dict
        self.closed_trades = []   # completed trades
        self.graduated_at = {}    # symbol -> unix timestamp when first seen in Pool A/B
        self.start_time = time.time()
        self._traded_symbols_ref: dict = {}  # set by caller to persist session traded_symbols

    def save_state(self, traded_symbols: dict | None = None):
        """Atomically persist positions, closed_trades, graduated_at, and traded_symbols."""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "positions": self.positions,
            "closed_trades": self.closed_trades,
            "graduated_at": self.graduated_at,
            "traded_symbols": traded_symbols if traded_symbols is not None else self._traded_symbols_ref,
        }
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, STATE_FILE)

    def load_state(self, traded_symbols_out: dict | None = None):
        """Load persisted state from STATE_FILE if it exists. Returns traded_symbols dict."""
        if not STATE_FILE.exists():
            return {}
        try:
            state = json.loads(STATE_FILE.read_text())
            self.positions = state.get("positions", {})
            self.closed_trades = state.get("closed_trades", [])
            self.graduated_at = state.get("graduated_at", {})
            print(f"  [State] Loaded {len(self.positions)} open position(s) and "
                  f"{len(self.closed_trades)} closed trade(s) from {STATE_FILE}")
            return state.get("traded_symbols", {})
        except Exception as e:
            print(f"  [State] WARNING: could not load state from {STATE_FILE}: {e}")
            return {}

    def enter(self, symbol, price, score, signals, trade_size=None):
        if symbol in self.positions:
            return
        size = trade_size if trade_size is not None else self.amount
        qty = size / price
        self.positions[symbol] = {
            "entry_price": price,
            "qty": qty,
            "trade_size": size,
            "entry_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "entry_unix": time.time(),
            "score": score,
        }
        print(f"  [PAPER] LONG {symbol} @ {price:,.6g} | "
              f"Score {score:.0f} | ${size:.0f} x{self.leverage}")
        self.save_state()

    def exit(self, symbol, current_price, reason):
        if symbol not in self.positions:
            return
        pos = self.positions.pop(symbol)
        entry = pos["entry_price"]
        size = pos.get("trade_size", self.amount)
        pnl_pct = (current_price - entry) / entry * 100
        pnl_usd = pnl_pct / 100 * size * self.leverage
        held = self._format_elapsed(time.time() - pos.get("entry_unix", time.time()))
        self.closed_trades.append({
            **pos,
            "exit_price": current_price,
            "exit_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "reason": reason,
            "symbol": symbol,
        })
        print(f"  [PAPER EXIT] {symbol} @ {current_price:,.6g} | "
              f"P&L: {pnl_pct:+.1f}% (${pnl_usd:+,.2f}) | Held: {held} | {reason}")
        self.save_state()
        shared_state.append_trade("accumulation", {
            "symbol": symbol,
            "entry_price": pos["entry_price"],
            "exit_price": current_price,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": round(pnl_usd, 2),
            "size_usdt": size,
            "reason": reason,
            "entry_time": pos.get("entry_unix", 0),
        })

    def update_prices(self, tickers_or_base_url):
        """Update prices for all open paper positions.

        Accepts either a pre-fetched list of ticker dicts (preferred) or a
        base_url string (legacy — will fetch tickers internally).
        """
        if not self.positions:
            return
        if isinstance(tickers_or_base_url, str):
            tickers = fetch_all_linear_tickers(tickers_or_base_url)
        else:
            tickers = tickers_or_base_url
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

    def display_positions(self):
        if not self.positions:
            return
        print(f"\n  {'─'*125}")
        print(f"  PAPER POSITIONS ({len(self.positions)} open)")
        print(f"  {'─'*125}")
        print(f"  {'Symbol':<14} {'Score':>6} {'Size':>8} {'Entry':>12} {'Current':>12}"
              f"  {'P&L%':>8}  {'P&L$':>10}  {'Entered':<22}  {'Held':>6}")
        print(f"  {'─'*125}")

        total_pnl = 0
        now = time.time()
        for symbol, pos in sorted(self.positions.items()):
            price = pos.get("current_price", pos["entry_price"])
            entry = pos["entry_price"]
            size = pos.get("trade_size", self.amount)
            pnl_pct = (price - entry) / entry * 100
            pnl_usd = pnl_pct / 100 * size
            total_pnl += pnl_usd
            held = self._format_elapsed(now - pos.get("entry_unix", now))

            print(f"  {symbol:<14} {pos['score']:>6.0f} ${size:>6.0f} {entry:>12,.6g} {price:>12,.6g}"
                  f"  {pnl_pct:>+7.1f}%  ${pnl_usd:>+9,.2f}  {pos['entry_time']:<22}  {held:>6}")

        print(f"  {'─'*125}")
        print(f"  {'Total unrealized P&L:':>95}  ${total_pnl:>+9,.2f}")
        print()

    def display_periodic_summary(self):
        unrealized = 0
        for pos in self.positions.values():
            price = pos.get("current_price", pos["entry_price"])
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
            unrealized += pnl_pct / 100 * pos.get("trade_size", self.amount)

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

        print(f"\n{'='*120}")
        print(f"  PAPER TRADING SUMMARY")
        print(f"  Session: {int(hours)}h {int(mins)}m | "
              f"Score-based sizing @ {self.leverage}x leverage | Pool D accumulation strategy")
        print(f"{'='*120}")

        all_trades = list(self.closed_trades)
        total_pnl = 0
        wins = 0
        losses = 0

        if self.closed_trades:
            print(f"\n  CLOSED TRADES ({len(self.closed_trades)}):")
            print(f"  {'─'*130}")
            print(f"  {'Symbol':<14} {'Score':>6} {'Entry':>12} {'Exit':>12}"
                  f"  {'P&L%':>8}  {'P&L$':>10}  {'Held':>6}  {'Exit Reason':<40}")
            print(f"  {'─'*130}")
            for t in self.closed_trades:
                held = self._format_elapsed(
                    (datetime.strptime(t["exit_time"], "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                     - datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                    ).total_seconds()) if "exit_time" in t else "?"
                total_pnl += t["pnl_usd"]
                if t["pnl_usd"] >= 0:
                    wins += 1
                else:
                    losses += 1
                print(f"  {t['symbol']:<14} {t['score']:>6.0f} {t['entry_price']:>12,.6g} {t['exit_price']:>12,.6g}"
                      f"  {t['pnl_pct']:>+7.1f}%  ${t['pnl_usd']:>+9,.2f}  {held:>6}  {t['reason']:<40}")

        if self.positions:
            print(f"\n  OPEN POSITIONS ({len(self.positions)}):")
            print(f"  {'─'*115}")
            print(f"  {'Symbol':<14} {'Score':>6} {'Entry':>12} {'Current':>12}"
                  f"  {'P&L%':>8}  {'P&L$':>10}  {'Entered':<22}  {'Held':>6}")
            print(f"  {'─'*115}")

            now = time.time()
            for symbol, pos in sorted(self.positions.items(), key=lambda x: x[1].get("current_price", x[1]["entry_price"]) / x[1]["entry_price"] - 1, reverse=True):
                price = pos.get("current_price", pos["entry_price"])
                entry = pos["entry_price"]
                pnl_pct = (price - entry) / entry * 100
                pnl_usd = pnl_pct / 100 * pos.get("trade_size", self.amount)
                total_pnl += pnl_usd
                if pnl_usd >= 0:
                    wins += 1
                else:
                    losses += 1
                held = self._format_elapsed(now - pos.get("entry_unix", now))
                print(f"  {symbol:<14} {pos['score']:>6.0f} {entry:>12,.6g} {price:>12,.6g}"
                      f"  {pnl_pct:>+7.1f}%  ${pnl_usd:>+9,.2f}  {pos['entry_time']:<22}  {held:>6}")

        if not self.positions and not self.closed_trades:
            print(f"\n  No paper trades were opened during this session.")
            print(f"{'='*120}\n")
            return

        total_trades = len(self.positions) + len(self.closed_trades)
        print(f"\n  {'─'*60}")
        print(f"  RESULTS:")
        print(f"    Total trades:  {total_trades} ({len(self.closed_trades)} closed, {len(self.positions)} open)")
        print(f"    Winning:       {wins} ({wins/total_trades*100:.0f}%)" if total_trades else "")
        print(f"    Losing:        {losses} ({losses/total_trades*100:.0f}%)" if total_trades else "")
        print(f"\n    TOTAL P&L:     ${total_pnl:+,.2f}")
        print(f"{'='*120}\n")


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

    # Only require API keys for live mode
    if args.live:
        api_key, api_secret = get_credentials()
    else:
        api_key = os.environ.get("BYBIT_API_KEY", "")
        api_secret = os.environ.get("BYBIT_API_SECRET", "")

    max_exposure = args.account_balance * args.max_exposure_mult
    session = TradingSession(
        max_per_cycle=MAX_TRADES_PER_CYCLE,
        max_per_day=MAX_TRADES_PER_DAY,
        max_exposure=max_exposure,
    )

    init_trade_log()

    paper = None if args.live else MomentumPaperTrader(args.amount, args.leverage)
    if paper is not None:
        saved_traded_symbols = paper.load_state()
        if saved_traded_symbols:
            session.traded_symbols.update(saved_traded_symbols)
        paper._traded_symbols_ref = session.traded_symbols

    base_size = args.account_balance / 10
    print(f"\n{'='*70}")
    print(f"  ACCUMULATION AUTO-TRADER [{env_label}] [{mode}]")
    print(f"{'='*70}")
    print(f"  Account balance: ${args.account_balance:,.0f}")
    print(f"  Sizing:          Score-based (${base_size*0.5:.0f}-${base_size*2:.0f} per trade)")
    print(f"  Leverage:        {args.leverage}x")
    print(f"  Max exposure:    ${max_exposure:,.0f} notional ({args.max_exposure_mult}x account)")
    print(f"  Strategy:        Pool D accumulation → exit on graduation")
    print(f"  Entry:           Pool D only (score 25+ AND accum signal 20+)")
    print(f"  Watch:           Pool A/B/C coins shown at score {args.min_score}+")
    print(f"  Exit:            Pool A/B graduation, funding flip, OI drop, 200%+ extension")
    print(f"  Scan interval:   every {args.interval} minutes")
    print(f"  Max per cycle:   {MAX_TRADES_PER_CYCLE} trades")
    print(f"  Max per day:     {MAX_TRADES_PER_DAY} trades")
    print(f"  Re-entry after:  {RE_ENTRY_COOLDOWN_HOURS}h cooldown")
    print(f"  Trade log:       {TRADE_LOG_FILE}")

    if not args.live:
        print(f"\n  >>> DRY-RUN MODE — paper trades tracked with P&L <<<")
        print(f"  >>> Add --live flag to enable real trading <<<")
        print(f"  >>> P&L summary shown after each scan. Ctrl+C for final summary <<<")
    else:
        print(f"\n  >>> LIVE MODE — REAL ORDERS WILL BE PLACED <<<")
        print(f"  >>> Score-based sizing on {env_label} <<<")

    if args.live:
        # Verify connection (only needed for live trading)
        print(f"\n  Verifying connection...")
        balance = check_balance(base_url, api_key, api_secret)
        if balance is None:
            print("  Failed to connect. Check your API credentials.")
            sys.exit(1)
        print(f"  Connected. Available balance: ${balance:,.2f} USDT")
        positions = get_open_positions(base_url, api_key, api_secret)
        print(f"  Open positions: {len(positions)}")
    else:
        print(f"\n  Paper mode — no API keys required. Using public data only.")
    print(f"{'='*70}\n")

    # Main loop
    cycle = 0
    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        print(f"\n--- Cycle {cycle} | {now} | {mode} ---\n")

        # ── EXIT CHECK: check open positions for graduation/exit signals ──
        open_symbols = list(paper.positions.keys()) if paper else []
        if not paper and args.live:
            live_positions = get_open_positions(base_url, api_key, api_secret)
            open_symbols = [p.get("symbol", "") for p in live_positions
                           if float(p.get("size", 0)) > 0]

        if open_symbols:
            print(f"  Checking exit signals for {len(open_symbols)} open position(s)...\n")
            # graduation_tracker shared between paper and live
            graduation_tracker = paper.graduated_at if paper else {}

            for sym in open_symbols:
                if paper and sym in paper.positions:
                    entry_price = paper.positions[sym]["entry_price"]
                    entry_unix = paper.positions[sym].get("entry_unix", 0)
                else:
                    entry_price = 0
                    entry_unix = 0
                    if args.live:
                        for p in live_positions:
                            if p.get("symbol") == sym:
                                entry_price = float(p.get("avgPrice", "0") or "0")
                                break

                if entry_price <= 0:
                    continue

                # Hard P&L exit — check BEFORE signals to catch dumps that never reach Pool A/B
                if paper and sym in paper.positions:
                    current_price = paper.positions[sym].get("current_price", entry_price)
                elif args.live:
                    current_price = 0.0
                    for p in live_positions:
                        if p.get("symbol") == sym:
                            current_price = float(p.get("markPrice", "0") or "0")
                            break
                else:
                    current_price = entry_price
                if current_price > 0:
                    pnl = (current_price - entry_price) / entry_price * 100
                    if pnl <= -15:
                        # Hard exit — P&L breach, don't wait for signals
                        paper.exit(sym, current_price, f"HARD EXIT: P&L {pnl:.1f}% breached -15% threshold")
                        continue

                graduated_since = graduation_tracker.get(sym)
                exit_info = check_exit_signals(base_url, sym, entry_price, graduated_since)
                pool_now = exit_info.get("pool", "?")
                pnl = exit_info.get("pnl_pct", 0)

                # Track graduation timestamp
                if exit_info.get("graduated") and sym not in graduation_tracker:
                    graduation_tracker[sym] = time.time()
                    grad_label = "JUST NOW"
                elif exit_info.get("graduated") and sym in graduation_tracker:
                    hrs = (time.time() - graduation_tracker[sym]) / 3600
                    grad_label = f"{hrs:.1f}h ago"
                elif not exit_info.get("graduated") and sym in graduation_tracker:
                    del graduation_tracker[sym]
                    grad_label = None
                else:
                    grad_label = None

                # Minimum hold: skip signal-based exits for first 30 min
                held_sec = time.time() - entry_unix
                if exit_info["exit"] and held_sec < MIN_HOLD_SECONDS:
                    mins_left = (MIN_HOLD_SECONDS - held_sec) / 60
                    print(f"  HOLD:  {sym} (Pool {pool_now}, {pnl:+.1f}%) — exit signal but min hold {mins_left:.0f}m remaining")
                elif exit_info["exit"]:
                    print(f"  EXIT SIGNAL: {sym} (Pool {pool_now}, {pnl:+.1f}%)")
                    print(f"    Reason: {exit_info['reason']}")

                    if paper:
                        paper.exit(sym, exit_info["current_price"], exit_info["reason"])
                        if sym in paper.graduated_at:
                            del paper.graduated_at[sym]
                    elif args.live:
                        for p in live_positions:
                            if p.get("symbol") == sym:
                                size = p.get("size", "0")
                                pos_idx = int(p.get("positionIdx", 0))
                                print(f"    Closing {size} {sym}...", end=" ")
                                close_params = {
                                    "category": "linear", "symbol": sym,
                                    "side": "Sell", "orderType": "Market",
                                    "qty": size, "positionIdx": pos_idx,
                                    "reduceOnly": True,
                                }
                                result = api_request(base_url, "POST", "/v5/order/create",
                                                     api_key, api_secret, close_params)
                                if result.get("retCode") == 0:
                                    print(f"CLOSED")
                                    log_trade(sym, "Sell", size, exit_info["current_price"],
                                              float(size) * exit_info["current_price"],
                                              args.leverage, 0, {}, "exit",
                                              result.get("result", {}).get("orderId", "N/A"), "live")
                                else:
                                    print(f"FAILED: {result.get('retMsg')}")
                                break
                        if sym in graduation_tracker:
                            del graduation_tracker[sym]
                else:
                    sig_count = len(exit_info.get("signals", []))
                    if grad_label:
                        print(f"  RIDE:  {sym} (Pool {pool_now}, {pnl:+.1f}%) — graduated {grad_label}, riding day 1 FOMO")
                    elif sig_count > 0:
                        print(f"  WATCH: {sym} (Pool {pool_now}, {pnl:+.1f}%) — {sig_count} early signal(s): {exit_info['reason']}")
                    else:
                        print(f"  HOLD:  {sym} (Pool {pool_now}, {pnl:+.1f}%) — no exit signals")
            print()

        # ── ENTRY SCAN: find new Pool D accumulation candidates ──
        results = run_scan(base_url, top_n=40, min_score=25)

        if not results:
            print(f"\n  No coins above score {args.min_score}. Waiting...\n")
        else:
            # FHE backtest: 25 catches it May 1 (day before pump, +134%).
            # Lower thresholds catch earlier but lock capital on dead days.
            # Scam coins pump and dump fast — late entry is better than early.
            pool_d_entry_threshold = 25
            pool_d_results = [r for r in results if r.get("pool") == "D"
                              and r["momentum_score"] >= pool_d_entry_threshold
                              and r["signals"].get("accumulation", {}).get("score", 0) >= 20]
            other_results = [r for r in results if r.get("pool") != "D"
                             and r["momentum_score"] >= args.min_score]

            all_pool_d = [r for r in results if r.get("pool") == "D"]
            if other_results:
                print(f"\n  {len(other_results)} coin(s) in Pool A/B/C (watch only, not trading):")
                for r in other_results[:5]:
                    print(f"    {r['pool']} {r['symbol']:<14} Score: {r['momentum_score']:.0f} | "
                          f"24h: {r['change24h']:+.1f}% | Vol: ${r['turnover24h']/1e6:,.1f}M")

            if all_pool_d and not pool_d_results:
                print(f"\n  {len(all_pool_d)} Pool D coins found but none above entry threshold:")
                for r in all_pool_d[:8]:
                    acc = r["signals"].get("accumulation", {})
                    print(f"    D {r['symbol']:<14} Score: {r['momentum_score']:.0f} | "
                          f"Accum: {acc.get('score',0):.0f} | {acc.get('phase','?')} | {r['change24h']:+.1f}% | ${r['turnover24h']/1e6:,.1f}M")
                print()

            if not pool_d_results:
                print(f"\n  No Pool D accumulation candidates above entry threshold. Waiting...\n")
            else:
                print(f"\n  {len(pool_d_results)} Pool D accumulation candidate(s):\n")

                trades_this_cycle = 0
                for r in pool_d_results:
                    if trades_this_cycle >= MAX_TRADES_PER_CYCLE:
                        print(f"  Max trades per cycle reached ({MAX_TRADES_PER_CYCLE}). Skipping rest.")
                        break

                    symbol = r["symbol"]
                    score = r["momentum_score"]
                    price = r["lastPrice"]
                    signals = r["signals"]

                    turnover = r.get("turnover24h", 0)
                    print(f"  >> {symbol} [Pool D] | Score: {score} | Price: ${price:,.6g} | 24h: {r['change24h']:+.1f}% | Vol: ${turnover/1e6:,.1f}M")
                    acc = signals.get("accumulation", {})
                    if acc.get("score", 0) > 0:
                        print(f"     ACCUMULATION:  {acc['detail']} (score {acc['score']}, phase: {acc.get('phase', '?')})")
                    print(f"     Vol: {signals['volume_anomaly']['detail']}")
                    print(f"     OI:  {signals['oi_surge']['detail']}")
                    pre = signals.get("pre_squeeze", {})
                    if pre.get("score", 0) > 0:
                        print(f"     PRE-SQUEEZE:   {pre['detail']} (score {pre['score']}, phase: {pre.get('phase', '?')})")
                    crime = signals.get("crime_pump", {})
                    if crime.get("crime_score", 0) > 0:
                        print(f"     Crime risk:    {crime['detail']} (score {crime['crime_score']})")

                    # Safety checks
                    can_trade, reason = session.can_trade(symbol)
                    if not can_trade:
                        print(f"     SKIP: {reason}")
                        continue

                    # Score-based sizing
                    trade_size = compute_trade_size(
                        score, args.account_balance, max_exposure,
                        session.total_exposure, args.leverage)
                    if trade_size < 5:
                        print(f"     SKIP: trade size too small (${trade_size:.0f}, exposure cap reached?)")
                        continue
                    print(f"     SIZE: ${trade_size:.0f} (score {score:.0f} → {trade_size/base_size:.1f}x base)")

                    # Get instrument info for qty precision
                    instrument = get_instrument_info(base_url, symbol)
                    if not instrument:
                        print(f"     SKIP: could not fetch instrument info")
                        continue

                    qty = calculate_qty(trade_size, price, instrument)
                    if not qty:
                        print(f"     SKIP: qty too small for ${trade_size:.0f} at ${price}")
                        continue

                    est_value = float(qty) * price
                    print(f"     Order: BUY {qty} {symbol} (~${est_value:,.2f}) @ {args.leverage}x leverage")

                    sl_price = price * 0.92

                    if not args.live:
                        print(f"     [DRY-RUN] Would place order")
                        log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                  score, signals, "dry-run", "N/A", "dry-run")
                        if paper:
                            paper.enter(symbol, price, score, signals, trade_size=trade_size)
                        session.record_trade(symbol, est_value * args.leverage)
                        trades_this_cycle += 1
                    else:
                        print(f"     Setting leverage to {args.leverage}x...", end=" ")
                        lev_ok = set_leverage(base_url, api_key, api_secret, symbol, args.leverage)
                        print("OK" if lev_ok else "WARN (may already be set)")

                        time.sleep(0.3)
                        print(f"     Placing market order...", end=" ")
                        result = place_market_order(base_url, api_key, api_secret, symbol, qty,
                                                    stop_loss=sl_price)
                        ret_code = result.get("retCode", -1)
                        order_id = result.get("result", {}).get("orderId", "N/A")

                        if ret_code == 0:
                            print(f"FILLED (orderId: {order_id})")
                            log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                      score, signals, "filled", order_id, "live")
                            session.record_trade(symbol, est_value * args.leverage)
                            trades_this_cycle += 1
                        elif ret_code == 10001 and "position idx" in result.get("retMsg", "").lower():
                            print(f"hedge mode detected, retrying...", end=" ")
                            time.sleep(0.3)
                            order_link_id = f"momentum_{symbol}_{int(time.time())}"
                            hedge_params = {
                                "category": "linear", "symbol": symbol,
                                "side": "Buy", "orderType": "Market", "qty": qty,
                                "orderLinkId": order_link_id, "positionIdx": 1,
                                "stopLoss": str(sl_price),
                            }
                            result2 = api_request(base_url, "POST", "/v5/order/create",
                                                  api_key, api_secret, hedge_params)
                            if result2.get("retCode") == 0:
                                oid = result2.get("result", {}).get("orderId", "N/A")
                                print(f"FILLED (orderId: {oid})")
                                log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                          score, signals, "filled", oid, "live")
                                session.record_trade(symbol, est_value * args.leverage)
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
            cycle_tickers = fetch_all_linear_tickers(base_url)
            paper.update_prices(cycle_tickers)
            paper.display_positions()
            paper.display_periodic_summary()
            # Publish positions to dashboard
            shared_state.write_positions("accumulation", [
                {"symbol": sym, "side": "long",
                 "entry_price": p["entry_price"],
                 "current_price": p.get("current_price", p["entry_price"]),
                 "pnl_pct": round((p.get("current_price", p["entry_price"]) - p["entry_price"]) / p["entry_price"] * 100, 2),
                 "size_usdt": p.get("trade_size", paper.amount),
                 "leverage": paper.leverage,
                 "entry_time": p.get("entry_unix", 0),
                 "score": p.get("score", 0)}
                for sym, p in paper.positions.items()
            ])
        elif args.live:
            display_live_pnl(base_url, api_key, api_secret)

        # Session summary
        print(f"\n  Session: {session.trades_today} trades today | "
              f"{len(session.traded_symbols)} unique coins | "
              f"${session.total_exposure:,.0f} exposure")

        print(f"\n  Next scan in {args.interval} min... (Ctrl+C to stop)")
        try:
            # Check exits every 3 minutes between full scans
            exit_check_interval = 180  # 3 minutes
            total_wait = args.interval * 60
            waited = 0
            while waited < total_wait:
                time.sleep(min(exit_check_interval, total_wait - waited))
                waited += exit_check_interval
                if waited < total_wait and paper and paper.positions:
                    # Quick exit check
                    print(f"\n  [Exit check — {(total_wait - waited)//60}m until next scan]")
                    tickers = fetch_all_linear_tickers(base_url)
                    paper.update_prices(tickers)
                    for sym in list(paper.positions):
                        pos = paper.positions[sym]
                        current_price = pos.get("current_price", pos["entry_price"])
                        pnl = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                        if pnl <= -15:
                            paper.exit(sym, current_price, f"HARD EXIT: P&L {pnl:.1f}% breached -15%")
                            continue
                        held_sec = time.time() - pos.get("entry_unix", 0)
                        if held_sec < MIN_HOLD_SECONDS:
                            continue
                        exit_info = check_exit_signals(base_url, sym, pos["entry_price"],
                                                       paper.graduated_at.get(sym))
                        if exit_info["graduated"] and sym not in paper.graduated_at:
                            paper.graduated_at[sym] = time.time()
                        if exit_info["exit"]:
                            paper.exit(sym, exit_info["current_price"], exit_info["reason"])
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
                        help=f"USDT amount per trade in fixed mode (default: ${DEFAULT_AMOUNT_USDT})")
    parser.add_argument("--account-balance", type=float, default=DEFAULT_ACCOUNT_BALANCE,
                        help=f"Account balance for score-based sizing (default: ${DEFAULT_ACCOUNT_BALANCE})")
    parser.add_argument("--max-exposure-mult", type=float, default=DEFAULT_MAX_EXPOSURE_MULT,
                        help=f"Max total exposure as multiple of account (default: {DEFAULT_MAX_EXPOSURE_MULT}x)")
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE,
                        help=f"Leverage multiplier (default: {DEFAULT_LEVERAGE}x)")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                        help=f"Minimum momentum score to trigger (default: {DEFAULT_MIN_SCORE})")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN,
                        help=f"Scan interval in minutes (default: {DEFAULT_INTERVAL_MIN})")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Skip interactive CONFIRM prompt (for dashboard/automation)")
    args = parser.parse_args()

    if args.live and not args.testnet and not args.no_confirm:
        print(f"\n  WARNING: You are about to run LIVE auto-trading on MAINNET.")
        print(f"  This will place REAL orders with REAL money.")
        base = args.account_balance / 10
        print(f"  Account: ${args.account_balance:,.0f} | Size: ${base*0.5:.0f}-${base*2:.0f} per trade | Leverage: {args.leverage}x")
        print(f"  Max exposure: ${args.account_balance * args.max_exposure_mult:,.0f} ({args.max_exposure_mult}x account)")
        confirm = input("\n  Type CONFIRM to proceed: ").strip()
        if confirm.upper() != "CONFIRM":
            print("  Cancelled.")
            sys.exit(0)

    run_auto_trader(args)


if __name__ == "__main__":
    main()
