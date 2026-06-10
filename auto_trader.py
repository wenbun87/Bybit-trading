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
DEFAULT_MAX_EXPOSURE_MULT = 10  # Max total exposure = balance × this (with leverage)
DEFAULT_MIN_SCORE = 40          # ELEVATED threshold (catch accumulation earlier)
DEFAULT_INTERVAL_MIN = 5        # scan every 5 minutes
MAX_TRADES_PER_CYCLE = 2        # max trades per scan cycle
MAX_TRADES_PER_DAY = 6          # max trades in 24 hours
DEFAULT_LEVERAGE = 5            # 5x leverage
RE_ENTRY_COOLDOWN_HOURS = 6     # allow re-entry on same symbol after this cooldown
MIN_HOLD_SECONDS = 1800         # 30 min minimum hold before signal-based exits (hard exit still active)
STOP_LOSS_PCT = -8              # hard stop loss — tighter to limit damage at 5x leverage
STALE_HOLD_HOURS = 72           # cut positions going nowhere after 3 days
STALE_PNL_RANGE = (-10, 5)      # only cut if P&L is between -10% and +5% (dead money zone)

# Lottery mode: tiny positions on likely cabal/scam coins, no stop, let it ride
# Crime score at entry = lifecycle stage indicator.
# Low (<30) = early, cabal still accumulating → full size.
# 30-60 = pump already starting, entering late → half size.
# 60+ = hard blocked by scanner (you'd be the exit liquidity).
CRIME_HALF_SIZE_THRESHOLD = 30

# Exit strategy (all positions): scale out at first target, let runner ride
SCALEOUT_PCT = 25               # take 50% off at +25%
SCALEOUT_RATIO = 0.5            # sell this fraction at first target
OI_DIVERGENCE_PCT = 20          # exit runner if OI drops 20%+ from peak while price near highs
FUNDING_DEEP_NEG_THRESHOLD = -0.003   # funding rate per cycle considered "deeply negative"
FUNDING_DECAY_RATIO = 0.30            # exit when funding decays to <30% of peak magnitude
RATCHET_TIERS = [               # (pnl_threshold, lock_floor) — safety net for runner
    (200, 80),                  # hit +200% → floor at +80%
    (100, 30),                  # hit +100% → floor at +30%
    (50,   0),                  # hit +50%  → floor at breakeven
]

# Score-based sizing tiers: (min_score, multiplier_of_base)
SCORE_SIZE_TIERS = [
    (90, 3.0),   # Exceptional → 3x base
    (80, 2.5),   # Very strong → 2.5x base
    (70, 2.0),   # Strong → 2x base
    (60, 1.5),   # Good → 1.5x base
    (50, 1.0),   # Moderate → 1x base
    (40, 0.75),  # Marginal → 0.75x base
]


def compute_trade_size(score: float, account_balance: float, max_exposure: float,
                       current_exposure: float, leverage: int) -> float:
    """Compute trade size (notional) based on signal score and account limits."""
    base_margin = account_balance / 5
    multiplier = 0.5
    for min_score, mult in SCORE_SIZE_TIERS:
        if score >= min_score:
            multiplier = mult
            break
    size = base_margin * multiplier * leverage
    remaining = max(0, max_exposure - current_exposure)
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
# Lottery exit helpers
# ──────────────────────────────────────────────

def check_oi_divergence(base_url: str, symbol: str, peak_oi: float,
                        current_pnl: float, peak_pnl: float) -> str | None:
    """Detect OI divergence: OI collapsing while price still near highs.
    Returns exit reason string or None."""
    from momentum_scanner import fetch_open_interest
    oi_data = fetch_open_interest(base_url, symbol)
    if not oi_data or len(oi_data) < 2:
        return None
    try:
        current_oi = float(oi_data[0].get("openInterest", 0))
    except (ValueError, TypeError):
        return None
    if peak_oi <= 0 or current_oi <= 0:
        return None
    oi_drop_pct = (peak_oi - current_oi) / peak_oi * 100
    price_near_highs = current_pnl >= peak_pnl * 0.70 if peak_pnl > 0 else False
    if oi_drop_pct >= OI_DIVERGENCE_PCT and price_near_highs:
        return (f"OI DIVERGENCE: OI down {oi_drop_pct:.0f}% from peak "
                f"while price still near highs ({current_pnl:+.0f}%)")
    return None


def check_structure_break(base_url: str, symbol: str) -> str | None:
    """Detect higher-low break on 1h candles: pump structure broken.
    Returns exit reason string or None."""
    from momentum_scanner import fetch_klines
    klines = fetch_klines(base_url, symbol, interval="60", limit=48)
    if not klines or len(klines) < 10:
        return None
    lows = []
    for i in range(2, len(klines) - 2):
        try:
            lo = float(klines[i][3])
            left_ok = float(klines[i-1][3]) >= lo and float(klines[i-2][3]) >= lo
            right_ok = float(klines[i+1][3]) >= lo and float(klines[i+2][3]) >= lo
            if left_ok and right_ok:
                lows.append((i, lo))
        except (ValueError, TypeError, IndexError):
            continue
    if len(lows) < 2:
        return None
    # Find the most recent higher low (swing low that is higher than its predecessor)
    higher_low = None
    for i in range(len(lows) - 1, 0, -1):
        if lows[i][1] > lows[i-1][1]:
            higher_low = lows[i][1]
            break
    if higher_low is None:
        return None
    try:
        current_close = float(klines[-1][4])
    except (ValueError, TypeError):
        return None
    if current_close < higher_low:
        return (f"STRUCTURE BREAK: 1h close {current_close:.6g} below "
                f"higher low {higher_low:.6g}")
    return None


def get_current_oi(base_url: str, symbol: str) -> float:
    """Fetch latest OI value for tracking peak."""
    from momentum_scanner import fetch_open_interest
    oi_data = fetch_open_interest(base_url, symbol)
    if oi_data:
        try:
            return float(oi_data[0].get("openInterest", 0))
        except (ValueError, TypeError):
            pass
    return 0.0


def get_current_funding(base_url: str, symbol: str) -> float | None:
    """Fetch latest funding rate. Returns rate per cycle (e.g. -0.025 = -2.5%)."""
    from momentum_scanner import fetch_funding_history
    records = fetch_funding_history(base_url, symbol)
    if records:
        try:
            return float(records[0].get("fundingRate", 0))
        except (ValueError, TypeError):
            pass
    return None


def check_funding_decay(base_url: str, symbol: str,
                        peak_neg_funding: float) -> str | None:
    """Detect funding normalization: deeply negative funding flattening out.
    Squeeze fuel exhausted — no more short liquidation cascades.
    Returns exit reason string or None."""
    if peak_neg_funding >= FUNDING_DEEP_NEG_THRESHOLD:
        return None
    current = get_current_funding(base_url, symbol)
    if current is None:
        return None
    peak_magnitude = abs(peak_neg_funding)
    current_magnitude = abs(min(current, 0))
    if current_magnitude < peak_magnitude * FUNDING_DECAY_RATIO:
        return (f"FUNDING DECAY: peak was {peak_neg_funding*100:+.2f}%/cycle, "
                f"now {current*100:+.2f}% — squeeze fuel exhausted")
    return None


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
            "peak_pnl_pct": 0.0,
            "peak_oi": 0.0,
            "peak_neg_funding": 0.0,
            "scaled_out": False,
            "original_trade_size": size,
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
        pnl_usd = pnl_pct / 100 * size
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
              f"P&L: {pnl_pct:+.1f}% (${pnl_usd:+,.2f}) | Size: ${size:.0f} | Held: {held} | {reason}")
        self.save_state()
        shared_state.append_trade("accumulation", {
            "symbol": symbol,
            "entry_price": pos["entry_price"],
            "exit_price": current_price,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": round(pnl_usd, 2),
            "size_usdt": size,
            "leverage": self.leverage,
            "reason": reason,
            "entry_time": pos.get("entry_unix", 0),
            "score": pos.get("score", 0),
        })

    def scale_out(self, symbol, current_price, ratio=SCALEOUT_RATIO):
        """Close a fraction of a position (partial take profit)."""
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        if pos.get("scaled_out"):
            return

        entry = pos["entry_price"]
        full_size = pos.get("trade_size", self.amount)
        exit_size = full_size * ratio
        pnl_pct = (current_price - entry) / entry * 100
        pnl_usd = pnl_pct / 100 * exit_size

        # Log the partial close as a trade
        self.closed_trades.append({
            **{k: v for k, v in pos.items() if k != "current_price"},
            "exit_price": current_price,
            "exit_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "reason": f"SCALE OUT: {ratio*100:.0f}% at +{pnl_pct:.0f}%",
            "symbol": symbol,
            "trade_size": exit_size,
        })
        shared_state.append_trade("accumulation", {
            "symbol": symbol,
            "entry_price": entry,
            "exit_price": current_price,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": round(pnl_usd, 2),
            "size_usdt": exit_size,
            "leverage": self.leverage,
            "reason": f"SCALE OUT: {ratio*100:.0f}% at +{pnl_pct:.0f}%",
            "entry_time": pos.get("entry_unix", 0),
            "score": pos.get("score", 0),
        })

        # Reduce position
        pos["trade_size"] = full_size - exit_size
        pos["qty"] = pos["qty"] * (1 - ratio)
        pos["scaled_out"] = True

        held = self._format_elapsed(time.time() - pos.get("entry_unix", time.time()))
        print(f"  [SCALE OUT] {symbol} — sold {ratio*100:.0f}% @ {current_price:,.6g} | "
              f"P&L: {pnl_pct:+.1f}% (${pnl_usd:+,.2f}) | "
              f"Runner: ${pos['trade_size']:.0f} | Held: {held}")
        self.save_state()

    def update_prices(self, tickers_or_base_url):
        """Update prices and peak P&L for all open paper positions."""
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
                pnl = (price - pos["entry_price"]) / pos["entry_price"] * 100
                if pnl > pos.get("peak_pnl_pct", 0):
                    pos["peak_pnl_pct"] = pnl

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
        print(f"\n  {'─'*130}")
        print(f"  PAPER POSITIONS ({len(self.positions)} open)")
        print(f"  {'─'*130}")
        print(f"  {'Symbol':<14} {'Score':>6} {'Size':>8} {'Entry':>12} {'Current':>12}"
              f"  {'P&L%':>8}  {'P&L$':>10}  {'Entered':<22}  {'Held':>6}")
        print(f"  {'─'*120}")

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

        print(f"  {'─'*130}")
        print(f"  {'Total unrealized P&L:':>100}  ${total_pnl:>+9,.2f}")
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



def _close_live_position(base_url, api_key, api_secret, sym, live_positions,
                         leverage, entry_price, entry_unix, current_price, pnl, reason,
                         live_tracker=None):
    """Close a live position on Bybit, record to trade history and live_tracker."""
    for p in live_positions:
        if p.get("symbol") != sym:
            continue
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
            pos_value = float(size) * current_price
            total_size = pos_value
            score = 0
            if live_tracker and sym in live_tracker.positions:
                total_size = live_tracker.positions[sym].get("trade_size", pos_value)
                score = live_tracker.positions[sym].get("score", 0)
                live_tracker.positions.pop(sym, None)
                live_tracker.save_state()
            log_trade(sym, "Sell", size, current_price, pos_value, leverage,
                      0, {}, "exit", result.get("result", {}).get("orderId", "N/A"), "live")
            shared_state.append_trade("accumulation", {
                "symbol": sym,
                "entry_price": entry_price,
                "exit_price": current_price,
                "pnl_pct": round(pnl, 2),
                "pnl_usd": round(pnl / 100 * total_size, 2),
                "size_usdt": total_size,
                "leverage": leverage,
                "reason": reason,
                "entry_time": entry_unix,
                "score": score,
            })
        else:
            print(f"FAILED: {result.get('retMsg')}")
        break


def _partial_close_live(base_url, api_key, api_secret, sym, live_positions,
                        ratio):
    """Close a fraction of a live position (reduce-only market sell).
    Trade recording is handled by the caller via tracker.scale_out()."""
    for p in live_positions:
        if p.get("symbol") != sym:
            continue
        full_qty = float(p.get("size", 0))
        if full_qty <= 0:
            break
        pos_idx = int(p.get("positionIdx", 0))

        instrument = get_instrument_info(base_url, sym)
        if instrument:
            lot_filter = instrument.get("lotSizeFilter", {})
            qty_step = float(lot_filter.get("qtyStep", "0.001"))
            min_qty = float(lot_filter.get("minOrderQty", "0.001"))
        else:
            qty_step = 0.001
            min_qty = 0.001

        raw_qty = full_qty * ratio
        steps = int(raw_qty / qty_step)
        partial_qty = steps * qty_step
        if partial_qty < min_qty:
            print(f"    Partial close skipped: qty {partial_qty} below minimum {min_qty}")
            break
        if qty_step >= 1:
            qty_str = str(int(partial_qty))
        else:
            decimals = len(str(qty_step).rstrip("0").split(".")[-1])
            qty_str = f"{partial_qty:.{decimals}f}"

        print(f"    Scaling out {qty_str} of {full_qty} {sym}...", end=" ")
        result = api_request(base_url, "POST", "/v5/order/create",
                             api_key, api_secret, {
                                 "category": "linear", "symbol": sym,
                                 "side": "Sell", "orderType": "Market",
                                 "qty": qty_str, "positionIdx": pos_idx,
                                 "reduceOnly": True,
                             })
        if result.get("retCode") == 0:
            print("DONE")
        else:
            print(f"FAILED: {result.get('retMsg')}")
        break


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
    # Live tracker mirrors paper interface for score/dashboard publishing
    live_tracker = MomentumPaperTrader(args.amount, args.leverage) if args.live else None
    tracker = paper or live_tracker
    if tracker is not None:
        saved_traded_symbols = tracker.load_state()
        if saved_traded_symbols:
            session.traded_symbols.update(saved_traded_symbols)
        tracker._traded_symbols_ref = session.traded_symbols
        # Restore exposure and daily trade count so limits work across restarts
        for pos in tracker.positions.values():
            session.total_exposure += pos.get("trade_size", 0)
        today = datetime.now(timezone.utc).date()
        for ts in session.traded_symbols.values():
            if datetime.fromtimestamp(ts, tz=timezone.utc).date() == today:
                session.trades_today += 1
        if tracker.positions or session.trades_today:
            print(f"  [State] Restored {len(tracker.positions)} position(s), "
                  f"${session.total_exposure:,.0f} exposure, "
                  f"{session.trades_today} trades today")

    base_size = args.account_balance / 5 * args.leverage
    print(f"\n{'='*70}")
    print(f"  ACCUMULATION AUTO-TRADER [{env_label}] [{mode}]")
    print(f"{'='*70}")
    print(f"  Account balance: ${args.account_balance:,.0f}")
    print(f"  Sizing:          Score-based (${base_size*0.5:.0f}-${base_size*3:.0f} notional per trade)")
    print(f"  Leverage:        {args.leverage}x")
    print(f"  Max exposure:    ${max_exposure:,.0f} notional ({args.max_exposure_mult}x account)")
    print(f"  Strategy:        Pool D accumulation → exit on graduation")
    print(f"  Entry:           Pool D only (score 40+ AND accum signal 20+)")
    print(f"  Watch:           Pool A/B/C coins shown at score {args.min_score}+")
    print(f"  Stop loss:       {STOP_LOSS_PCT}%")
    print(f"  Stale exit:      Cut after {STALE_HOLD_HOURS}h if P&L in [{STALE_PNL_RANGE[0]}%, {STALE_PNL_RANGE[1]}%]")
    print(f"  Late entry:      Crime score {CRIME_HALF_SIZE_THRESHOLD}+ → half size (pump already started)")
    print(f"  Exit strategy:   Scale out {SCALEOUT_RATIO*100:.0f}% at +{SCALEOUT_PCT}%, runner rides")
    print(f"  Runner exits:    OI divergence, 1h structure break, ratchet floors")
    print(f"  Pre-target exit: Pool A/B graduation, funding flip, OI drop, crime re-flag")
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
        open_symbols = list(tracker.positions.keys()) if tracker else []
        live_positions = get_open_positions(base_url, api_key, api_secret) if args.live else []

        # Fetch fresh prices for both modes so exit checks use current data
        if open_symbols and tracker:
            exit_tickers = fetch_all_linear_tickers(base_url)
            tracker.update_prices(exit_tickers)

        if open_symbols:
            print(f"  Checking exit signals for {len(open_symbols)} open position(s)...\n")
            graduation_tracker = tracker.graduated_at if tracker else {}

            for sym in open_symbols:
                if tracker and sym in tracker.positions:
                    entry_price = tracker.positions[sym]["entry_price"]
                    entry_unix = tracker.positions[sym].get("entry_unix", 0)
                else:
                    entry_price = 0
                    entry_unix = 0

                if entry_price <= 0:
                    continue

                # Use fresh tracker price (just updated above)
                pos_data = tracker.positions.get(sym, {}) if tracker else {}
                current_price = pos_data.get("current_price", entry_price)
                if current_price > 0:
                    pnl = (current_price - entry_price) / entry_price * 100
                    held_hours = (time.time() - entry_unix) / 3600

                    # Hard stop loss
                    if pnl <= STOP_LOSS_PCT:
                        reason = f"HARD EXIT: P&L {pnl:.1f}% breached {STOP_LOSS_PCT}% stop loss"
                        if not args.live:
                            tracker.exit(sym, current_price, reason)
                        else:
                            _close_live_position(base_url, api_key, api_secret, sym,
                                                 live_positions, args.leverage, entry_price,
                                                 entry_unix, current_price, pnl, reason, live_tracker)
                        continue

                    # Stale position exit: cut dead money after 3 days
                    if held_hours >= STALE_HOLD_HOURS and STALE_PNL_RANGE[0] <= pnl <= STALE_PNL_RANGE[1]:
                        reason = f"STALE: {pnl:+.1f}% after {held_hours:.0f}h — cutting dead money"
                        if not args.live:
                            tracker.exit(sym, current_price, reason)
                        else:
                            _close_live_position(base_url, api_key, api_secret, sym,
                                                 live_positions, args.leverage, entry_price,
                                                 entry_unix, current_price, pnl, reason, live_tracker)
                        continue

                # ── Scale-out + runner exits (all positions) ──
                if tracker and pos_data:
                    # Update peak OI tracking
                    cur_oi = get_current_oi(base_url, sym)
                    if cur_oi > pos_data.get("peak_oi", 0):
                        pos_data["peak_oi"] = cur_oi

                    # Update peak negative funding tracking
                    cur_funding = get_current_funding(base_url, sym)
                    if cur_funding is not None and cur_funding < pos_data.get("peak_neg_funding", 0):
                        pos_data["peak_neg_funding"] = cur_funding

                    # Scale out: sell 50% at +25%
                    if pnl >= SCALEOUT_PCT and not pos_data.get("scaled_out"):
                        if args.live:
                            _partial_close_live(base_url, api_key, api_secret, sym,
                                                live_positions, SCALEOUT_RATIO)
                        tracker.scale_out(sym, current_price)
                        continue

                    # Runner exits (only after scaled out)
                    if pos_data.get("scaled_out"):
                        # OI divergence: OI collapsing while price near highs
                        oi_reason = check_oi_divergence(
                            base_url, sym, pos_data.get("peak_oi", 0),
                            pnl, pos_data.get("peak_pnl_pct", 0))
                        if oi_reason:
                            if not args.live:
                                tracker.exit(sym, current_price, oi_reason)
                            else:
                                _close_live_position(base_url, api_key, api_secret, sym,
                                                     live_positions, args.leverage, entry_price,
                                                     entry_unix, current_price, pnl, oi_reason, live_tracker)
                            continue

                        # Funding decay: deeply negative funding flattening out
                        fund_reason = check_funding_decay(
                            base_url, sym, pos_data.get("peak_neg_funding", 0))
                        if fund_reason:
                            if not args.live:
                                tracker.exit(sym, current_price, fund_reason)
                            else:
                                _close_live_position(base_url, api_key, api_secret, sym,
                                                     live_positions, args.leverage, entry_price,
                                                     entry_unix, current_price, pnl, fund_reason, live_tracker)
                            continue

                        # Structure break: 1h close below most recent higher low
                        struct_reason = check_structure_break(base_url, sym)
                        if struct_reason:
                            if not args.live:
                                tracker.exit(sym, current_price, struct_reason)
                            else:
                                _close_live_position(base_url, api_key, api_secret, sym,
                                                     live_positions, args.leverage, entry_price,
                                                     entry_unix, current_price, pnl, struct_reason, live_tracker)
                            continue

                        # Ratchet floors: safety net for runners
                        for threshold, floor in RATCHET_TIERS:
                            if pos_data.get("peak_pnl_pct", 0) >= threshold and pnl <= floor:
                                reason = (f"RATCHET: peak was +{pos_data['peak_pnl_pct']:.0f}%, "
                                          f"now {pnl:+.1f}% — floor +{floor}% triggered")
                                if not args.live:
                                    tracker.exit(sym, current_price, reason)
                                else:
                                    _close_live_position(base_url, api_key, api_secret, sym,
                                                         live_positions, args.leverage, entry_price,
                                                         entry_unix, current_price, pnl, reason, live_tracker)
                                break
                        else:
                            peak = pos_data.get("peak_pnl_pct", 0)
                            print(f"  RIDE:  {sym} [RUNNER] {pnl:+.1f}% (peak {peak:+.0f}%) — letting it ride")
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

                    if not args.live:
                        tracker.exit(sym, exit_info["current_price"], exit_info["reason"])
                    else:
                        _close_live_position(base_url, api_key, api_secret, sym,
                                             live_positions, args.leverage, entry_price,
                                             entry_unix, exit_info["current_price"], pnl,
                                             exit_info["reason"], live_tracker)
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
            pool_d_entry_threshold = 40
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
                    if tracker and symbol in tracker.positions:
                        print(f"     SKIP: already holding {symbol}")
                        continue
                    can_trade, reason = session.can_trade(symbol)
                    if not can_trade:
                        print(f"     SKIP: {reason}")
                        continue

                    # Score-based sizing, halved if entering late (pump already started)
                    crime_score = signals.get("crime_pump", {}).get("crime_score", 0)
                    trade_size = compute_trade_size(
                        score, args.account_balance, max_exposure,
                        session.total_exposure, args.leverage)
                    if crime_score >= CRIME_HALF_SIZE_THRESHOLD:
                        trade_size = round(trade_size / 2, 2)
                        print(f"     LATE ENTRY: crime score {crime_score:.0f} → half size")
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

                    sl_price = price * (1 + STOP_LOSS_PCT / 100)

                    if not args.live:
                        # Paper: log and track
                        print(f"     [DRY-RUN] Would place order")
                        log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                  score, signals, "dry-run", "N/A", "dry-run")
                        tracker.enter(symbol, price, score, signals,
                                      trade_size=trade_size)
                        session.record_trade(symbol, est_value)
                        trades_this_cycle += 1
                    else:
                        # Live: place order on exchange, then track identically
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
                            tracker.enter(symbol, price, score, signals,
                                          trade_size=trade_size)
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
                            if sl_price is not None:
                                hedge_params["stopLoss"] = str(sl_price)
                            result2 = api_request(base_url, "POST", "/v5/order/create",
                                                  api_key, api_secret, hedge_params)
                            if result2.get("retCode") == 0:
                                oid = result2.get("result", {}).get("orderId", "N/A")
                                print(f"FILLED (orderId: {oid})")
                                log_trade(symbol, "Buy", qty, price, est_value, args.leverage,
                                          score, signals, "filled", oid, "live")
                                tracker.enter(symbol, price, score, signals,
                                              trade_size=trade_size)
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

        # P&L display — unified for paper and live
        if tracker:
            cycle_tickers = fetch_all_linear_tickers(base_url)
            tracker.update_prices(cycle_tickers)
            tracker.display_positions()
            tracker.display_periodic_summary()
            shared_state.write_positions("accumulation", [
                {"symbol": sym, "side": "long",
                 "entry_price": p["entry_price"],
                 "current_price": p.get("current_price", p["entry_price"]),
                 "pnl_pct": round((p.get("current_price", p["entry_price"]) - p["entry_price"]) / p["entry_price"] * 100, 2),
                 "size_usdt": p.get("trade_size", tracker.amount),
                 "leverage": tracker.leverage,
                 "entry_time": p.get("entry_unix", 0),
                 "score": p.get("score", 0),
                 }
                for sym, p in tracker.positions.items()
            ])

        # Session summary
        print(f"\n  Session: {session.trades_today} trades today | "
              f"{len(session.traded_symbols)} unique coins | "
              f"${session.total_exposure:,.0f} exposure")

        print(f"\n  Next scan in {args.interval} min... (Ctrl+C to stop)")
        try:
            # Check exits every 1 minute between full scans
            exit_check_interval = 60  # 1 minute
            total_wait = args.interval * 60
            waited = 0
            while waited < total_wait:
                time.sleep(min(exit_check_interval, total_wait - waited))
                waited += exit_check_interval
                if waited < total_wait and tracker and tracker.positions:
                    print(f"\n  [Price check — {(total_wait - waited)//60}m until next scan]")
                    tickers = fetch_all_linear_tickers(base_url)
                    tracker.update_prices(tickers)
                    # Update dashboard positions
                    shared_state.write_positions("accumulation", [
                        {"symbol": sym, "side": "long",
                         "entry_price": p["entry_price"],
                         "current_price": p.get("current_price", p["entry_price"]),
                         "pnl_pct": round((p.get("current_price", p["entry_price"]) - p["entry_price"]) / p["entry_price"] * 100, 2),
                         "size_usdt": p.get("trade_size", tracker.amount),
                         "leverage": tracker.leverage,
                         "entry_time": p.get("entry_unix", 0),
                         "score": p.get("score", 0),
                         }
                        for sym, p in tracker.positions.items()
                    ])
                    # Fetch live positions for exit execution
                    live_pos_check = get_open_positions(base_url, api_key, api_secret) if args.live else []
                    for sym in list(tracker.positions):
                        pos = tracker.positions[sym]
                        current_price = pos.get("current_price", pos["entry_price"])
                        entry_price = pos["entry_price"]
                        entry_unix = pos.get("entry_unix", 0)
                        pnl = (current_price - entry_price) / entry_price * 100
                        held_hours = (time.time() - entry_unix) / 3600

                        # Hard stop
                        if pnl <= STOP_LOSS_PCT:
                            reason = f"HARD EXIT: P&L {pnl:.1f}% breached {STOP_LOSS_PCT}% stop loss"
                            if not args.live:
                                tracker.exit(sym, current_price, reason)
                            else:
                                _close_live_position(base_url, api_key, api_secret, sym,
                                                     live_pos_check, args.leverage,
                                                     entry_price, entry_unix,
                                                     current_price, pnl, reason, live_tracker)
                            continue

                        # Stale exit: cut dead money after 3 days
                        if held_hours >= STALE_HOLD_HOURS and STALE_PNL_RANGE[0] <= pnl <= STALE_PNL_RANGE[1]:
                            reason = f"STALE: {pnl:+.1f}% after {held_hours:.0f}h — cutting dead money"
                            if not args.live:
                                tracker.exit(sym, current_price, reason)
                            else:
                                _close_live_position(base_url, api_key, api_secret, sym,
                                                     live_pos_check, args.leverage,
                                                     entry_price, entry_unix,
                                                     current_price, pnl, reason, live_tracker)
                            continue

                        # Scale-out + runner management (same logic as main cycle)
                        cur_oi = get_current_oi(base_url, sym)
                        if cur_oi > pos.get("peak_oi", 0):
                            pos["peak_oi"] = cur_oi

                        cur_funding = get_current_funding(base_url, sym)
                        if cur_funding is not None and cur_funding < pos.get("peak_neg_funding", 0):
                            pos["peak_neg_funding"] = cur_funding

                        if pnl >= SCALEOUT_PCT and not pos.get("scaled_out"):
                            if args.live:
                                _partial_close_live(base_url, api_key, api_secret, sym,
                                                    live_pos_check, SCALEOUT_RATIO)
                            tracker.scale_out(sym, current_price)
                            continue

                        if pos.get("scaled_out"):
                            oi_reason = check_oi_divergence(
                                base_url, sym, pos.get("peak_oi", 0),
                                pnl, pos.get("peak_pnl_pct", 0))
                            if oi_reason:
                                if not args.live:
                                    tracker.exit(sym, current_price, oi_reason)
                                else:
                                    _close_live_position(base_url, api_key, api_secret, sym,
                                                         live_pos_check, args.leverage,
                                                         entry_price, entry_unix,
                                                         current_price, pnl, oi_reason, live_tracker)
                                continue

                            fund_reason = check_funding_decay(
                                base_url, sym, pos.get("peak_neg_funding", 0))
                            if fund_reason:
                                if not args.live:
                                    tracker.exit(sym, current_price, fund_reason)
                                else:
                                    _close_live_position(base_url, api_key, api_secret, sym,
                                                         live_pos_check, args.leverage,
                                                         entry_price, entry_unix,
                                                         current_price, pnl, fund_reason, live_tracker)
                                continue

                            struct_reason = check_structure_break(base_url, sym)
                            if struct_reason:
                                if not args.live:
                                    tracker.exit(sym, current_price, struct_reason)
                                else:
                                    _close_live_position(base_url, api_key, api_secret, sym,
                                                         live_pos_check, args.leverage,
                                                         entry_price, entry_unix,
                                                         current_price, pnl, struct_reason, live_tracker)
                                continue

                            for threshold, floor in RATCHET_TIERS:
                                if pos.get("peak_pnl_pct", 0) >= threshold and pnl <= floor:
                                    reason = (f"RATCHET: peak was +{pos['peak_pnl_pct']:.0f}%, "
                                              f"now {pnl:+.1f}% — floor +{floor}% triggered")
                                    if not args.live:
                                        tracker.exit(sym, current_price, reason)
                                    else:
                                        _close_live_position(base_url, api_key, api_secret, sym,
                                                             live_pos_check, args.leverage,
                                                             entry_price, entry_unix,
                                                             current_price, pnl, reason, live_tracker)
                                    break
                            continue

                        held_sec = time.time() - entry_unix
                        if held_sec < MIN_HOLD_SECONDS:
                            continue
                        exit_info = check_exit_signals(base_url, sym, pos["entry_price"],
                                                       tracker.graduated_at.get(sym))
                        if exit_info["graduated"] and sym not in tracker.graduated_at:
                            tracker.graduated_at[sym] = time.time()
                        if exit_info["exit"]:
                            if not args.live:
                                tracker.exit(sym, exit_info["current_price"], exit_info["reason"])
                            else:
                                _close_live_position(base_url, api_key, api_secret, sym,
                                                     live_pos_check, args.leverage,
                                                     pos["entry_price"], pos.get("entry_unix", 0),
                                                     exit_info["current_price"],
                                                     exit_info.get("pnl_pct", 0),
                                                     exit_info["reason"], live_tracker)
        except KeyboardInterrupt:
            if tracker:
                tracker.display_summary()
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
        base = args.account_balance / 5 * args.leverage
        print(f"  Account: ${args.account_balance:,.0f} | Size: ${base*0.75:.0f}-${base*3:.0f} notional per trade | Leverage: {args.leverage}x")
        print(f"  Max exposure: ${args.account_balance * args.max_exposure_mult:,.0f} ({args.max_exposure_mult}x account)")
        confirm = input("\n  Type CONFIRM to proceed: ").strip()
        if confirm.upper() != "CONFIRM":
            print("  Cancelled.")
            sys.exit(0)

    run_auto_trader(args)


if __name__ == "__main__":
    main()
