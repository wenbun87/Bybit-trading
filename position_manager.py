#!/usr/bin/env python3
"""
Bybit Position Manager — Manage exits with adaptive trailing stops.

Monitors all open linear perpetual positions and applies a tiered
trailing stop strategy designed to let big winners run:

  1. New position → set initial stop loss (-5%)
  2. Profit  10%+ → activate trailing stop (5% distance)
  3. Profit  30%+ → tighten trail to 3%
  4. Profit 100%+ → tighten trail to 2%

This lets you ride a 20x move — the trailing stop follows the price
up and only closes when there's a small pullback from the peak.

Run this ALONGSIDE auto_trader.py — it manages the exits while
the auto trader handles entries.

REQUIRES:
  export BYBIT_API_KEY="your_key"
  export BYBIT_API_SECRET="your_secret"

Usage:
    python3 position_manager.py                # check every 2 min (dry-run)
    python3 position_manager.py --live         # actually set stops on Bybit
    python3 position_manager.py --live --interval 1   # check every 1 min
    python3 position_manager.py --live --initial-sl 8  # 8% initial stop loss
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

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

MAINNET_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
USER_AGENT = "bybit-skill/1.2.3"
RECV_WINDOW = "5000"

# Trailing stop tiers
# (min_profit_pct, trailing_stop_pct)
# Applied in order — last matching tier wins
TRAILING_TIERS = [
    (0,    0),     # below 10%: no trailing stop, just initial SL
    (10,   5.0),   # 10%+ profit: trail at 5% distance
    (30,   3.0),   # 30%+ profit: tighten to 3%
    (100,  2.0),   # 100%+ profit: tighten to 2%
    (300,  1.5),   # 300%+ profit: very tight 1.5%
]

DEFAULT_INITIAL_SL_PCT = 5.0    # initial stop loss: -5% from entry
DEFAULT_CHECK_INTERVAL = 2      # check every 2 minutes
MIN_API_INTERVAL = 0.15         # 150ms between API calls

EXIT_LOG_FILE = "exit_log.csv"
STATE_FILE = "position_state.json"

# ──────────────────────────────────────────────
# Authenticated API client
# ──────────────────────────────────────────────

_last_call_ts = 0.0


def get_credentials():
    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        print("\n  ERROR: BYBIT_API_KEY and BYBIT_API_SECRET must be set.")
        sys.exit(1)
    return api_key, api_secret


def sign_request(api_key, api_secret, timestamp, params_str):
    sign_str = f"{timestamp}{api_key}{RECV_WINDOW}{params_str}"
    return hmac.new(
        api_secret.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def api_request(base_url, method, path, api_key, api_secret, params=None):
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < MIN_API_INTERVAL:
        time.sleep(MIN_API_INTERVAL - elapsed)

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
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")

    req.add_header("X-BAPI-API-KEY", api_key)
    req.add_header("X-BAPI-TIMESTAMP", timestamp)
    req.add_header("X-BAPI-SIGN", sign)
    req.add_header("X-BAPI-RECV-WINDOW", RECV_WINDOW)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("X-Referer", "bybit-skill")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _last_call_ts = time.time()
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return {"retCode": -1, "retMsg": f"HTTP {e.code}: {error_body[:200]}"}
    except urllib.error.URLError as e:
        return {"retCode": -1, "retMsg": str(e)}


# ──────────────────────────────────────────────
# Position queries
# ──────────────────────────────────────────────

def get_open_positions(base_url, api_key, api_secret):
    """Fetch all open linear positions with size > 0."""
    data = api_request(base_url, "GET", "/v5/position/list",
                       api_key, api_secret,
                       {"category": "linear", "settleCoin": "USDT"})
    if data.get("retCode") != 0:
        print(f"  Error fetching positions: {data.get('retMsg')}")
        return []
    positions = data.get("result", {}).get("list", [])
    return [p for p in positions if float(p.get("size", "0") or "0") > 0]


def get_ticker_price(base_url, symbol):
    """Get current mark price for a symbol (public endpoint)."""
    url = f"{base_url}/v5/market/tickers?category=linear&symbol={symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            items = data.get("result", {}).get("list", [])
            if items:
                return float(items[0].get("lastPrice", 0))
    except Exception:
        pass
    return 0.0


# ──────────────────────────────────────────────
# Stop loss / trailing stop management
# ──────────────────────────────────────────────

def set_trading_stop(base_url, api_key, api_secret, symbol, position_idx,
                     stop_loss=None, trailing_stop=None):
    """
    Set stop loss and/or trailing stop on a position.
    trailing_stop is the distance in price (not percentage).
    """
    params = {
        "category": "linear",
        "symbol": symbol,
        "positionIdx": position_idx,
    }
    if stop_loss is not None:
        params["stopLoss"] = str(stop_loss)
    if trailing_stop is not None:
        params["trailingStop"] = str(trailing_stop)

    return api_request(base_url, "POST", "/v5/position/trading-stop",
                       api_key, api_secret, params)


def calculate_sl_price(entry_price, side, sl_pct):
    """Calculate stop loss price."""
    if side == "Buy":  # long position
        return round(entry_price * (1 - sl_pct / 100), 6)
    else:  # short position
        return round(entry_price * (1 + sl_pct / 100), 6)


def calculate_trailing_distance(current_price, trail_pct):
    """Calculate trailing stop distance in price."""
    return round(current_price * trail_pct / 100, 6)


def get_current_tier(profit_pct):
    """Determine which trailing stop tier applies."""
    active_tier = TRAILING_TIERS[0]
    for min_profit, trail_pct in TRAILING_TIERS:
        if profit_pct >= min_profit:
            active_tier = (min_profit, trail_pct)
    return active_tier


# ──────────────────────────────────────────────
# Position state persistence
# ──────────────────────────────────────────────

def load_state():
    """Load position management state from disk."""
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    """Save position management state to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ──────────────────────────────────────────────
# Exit logging
# ──────────────────────────────────────────────

def init_exit_log():
    if not Path(EXIT_LOG_FILE).exists():
        with open(EXIT_LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "symbol", "side", "entry_price", "exit_trigger",
                "profit_pct", "trailing_tier", "action",
            ])


def log_exit_event(symbol, side, entry_price, trigger, profit_pct, tier, action):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(EXIT_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([now, symbol, side, entry_price, trigger, profit_pct, tier, action])


# ──────────────────────────────────────────────
# Main position management loop
# ──────────────────────────────────────────────

def run_manager(args):
    base_url = TESTNET_URL if args.testnet else MAINNET_URL
    env_label = "TESTNET" if args.testnet else "MAINNET"
    mode = "LIVE" if args.live else "DRY-RUN"

    api_key, api_secret = get_credentials()
    state = load_state()
    init_exit_log()

    print(f"\n{'='*70}")
    print(f"  POSITION MANAGER [{env_label}] [{mode}]")
    print(f"{'='*70}")
    print(f"  Initial stop loss:  {args.initial_sl}%")
    print(f"  Trailing tiers:")
    for min_p, trail in TRAILING_TIERS:
        if trail > 0:
            print(f"    Profit {min_p}%+ → trail at {trail}% distance")
    print(f"  Check interval:    every {args.interval} min")
    print(f"  Exit log:          {EXIT_LOG_FILE}")

    if not args.live:
        print(f"\n  >>> DRY-RUN MODE — no stops will be modified <<<")
        print(f"  >>> Add --live flag to manage real positions <<<")
    else:
        print(f"\n  >>> LIVE MODE — will set real stop losses and trailing stops <<<")

    print(f"{'='*70}\n")

    cycle = 0
    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        print(f"--- Check #{cycle} | {now} | {mode} ---\n")

        positions = get_open_positions(base_url, api_key, api_secret)

        if not positions:
            print("  No open positions.\n")
            # Clean up state for closed positions
            state = {}
            save_state(state)
        else:
            print(f"  {len(positions)} open position(s):\n")

            active_symbols = set()

            for pos in positions:
                symbol = pos.get("symbol", "")
                side = pos.get("side", "")
                size = float(pos.get("size", "0") or "0")
                entry_price = float(pos.get("avgPrice", "0") or "0")
                mark_price = float(pos.get("markPrice", "0") or "0")
                unrealised_pnl = float(pos.get("unrealisedPnl", "0") or "0")
                position_idx = int(pos.get("positionIdx", "0") or "0")
                current_sl = float(pos.get("stopLoss", "0") or "0")
                current_trail = float(pos.get("trailingStop", "0") or "0")
                leverage = pos.get("leverage", "?")

                if entry_price <= 0 or mark_price <= 0:
                    continue

                active_symbols.add(symbol)

                # Calculate profit %
                if side == "Buy":
                    profit_pct = (mark_price - entry_price) / entry_price * 100
                else:
                    profit_pct = (entry_price - mark_price) / entry_price * 100

                # Leveraged PnL
                lev = float(leverage) if leverage != "?" else 1
                leveraged_pnl_pct = profit_pct * lev

                pos_value = size * mark_price
                state_key = f"{symbol}_{side}"

                # Get current tier
                _, tier_trail_pct = get_current_tier(profit_pct)

                # Display position
                pnl_str = f"{profit_pct:+.2f}% ({leveraged_pnl_pct:+.1f}% lev)"
                sl_str = f"${current_sl:,.6g}" if current_sl > 0 else "NONE"
                trail_str = f"${current_trail:,.6g}" if current_trail > 0 else "OFF"
                tier_str = f"{tier_trail_pct}%" if tier_trail_pct > 0 else "SL only"

                print(f"  {symbol} {side} | {size} @ ${entry_price:,.6g} | "
                      f"Mark: ${mark_price:,.6g} | PnL: {pnl_str}")
                print(f"    Value: ${pos_value:,.2f} | {leverage}x | "
                      f"SL: {sl_str} | Trail: {trail_str} | Tier: {tier_str}")

                # ── Decide what action to take ──

                action = None
                new_sl = None
                new_trail = None

                # Track what we've already done for this position
                pos_state = state.get(state_key, {
                    "initial_sl_set": False,
                    "current_tier_pct": 0,
                    "highest_profit": 0,
                })

                # Update highest profit seen
                if profit_pct > pos_state.get("highest_profit", 0):
                    pos_state["highest_profit"] = profit_pct

                # 1. Set initial SL if not set
                if not pos_state["initial_sl_set"] and current_sl == 0:
                    new_sl = calculate_sl_price(entry_price, side, args.initial_sl)
                    action = f"SET initial SL at ${new_sl:,.6g} (-{args.initial_sl}%)"
                    pos_state["initial_sl_set"] = True

                # 2. Upgrade trailing stop based on profit tier
                elif tier_trail_pct > 0 and tier_trail_pct != pos_state.get("current_tier_pct", 0):
                    # Only tighten, never widen
                    if tier_trail_pct < pos_state.get("current_tier_pct", 999):
                        new_trail = calculate_trailing_distance(mark_price, tier_trail_pct)
                        action = f"TIGHTEN trail to {tier_trail_pct}% (${new_trail:,.6g} distance)"
                        pos_state["current_tier_pct"] = tier_trail_pct
                    elif pos_state.get("current_tier_pct", 0) == 0:
                        # First time activating trailing stop
                        new_trail = calculate_trailing_distance(mark_price, tier_trail_pct)
                        action = f"ACTIVATE trail at {tier_trail_pct}% (${new_trail:,.6g} distance)"
                        pos_state["current_tier_pct"] = tier_trail_pct

                if action:
                    print(f"    >> {action}")

                    if args.live:
                        result = set_trading_stop(
                            base_url, api_key, api_secret, symbol, position_idx,
                            stop_loss=new_sl,
                            trailing_stop=new_trail,
                        )
                        ret = result.get("retCode", -1)
                        if ret == 0:
                            print(f"    >> APPLIED successfully")
                            log_exit_event(symbol, side, entry_price, "tier_update",
                                           profit_pct, tier_trail_pct, action)
                        else:
                            print(f"    >> FAILED: {result.get('retMsg')}")
                            # If position idx error, try hedge mode
                            if ret == 10001 and "position idx" in result.get("retMsg", "").lower():
                                alt_idx = 1 if side == "Buy" else 2
                                print(f"    >> Retrying with positionIdx={alt_idx}...")
                                time.sleep(0.3)
                                result2 = set_trading_stop(
                                    base_url, api_key, api_secret, symbol, alt_idx,
                                    stop_loss=new_sl, trailing_stop=new_trail,
                                )
                                if result2.get("retCode") == 0:
                                    print(f"    >> APPLIED successfully (hedge mode)")
                                else:
                                    print(f"    >> FAILED: {result2.get('retMsg')}")
                    else:
                        print(f"    >> [DRY-RUN] Would apply — skipping")
                        log_exit_event(symbol, side, entry_price, "dry_run",
                                       profit_pct, tier_trail_pct, action)
                else:
                    print(f"    >> No action needed")

                state[state_key] = pos_state
                print()

            # Clean up state for positions that closed
            closed = [k for k in state if k.split("_")[0] not in active_symbols]
            for k in closed:
                print(f"  Position closed: {k}")
                del state[k]

            save_state(state)

        print(f"  Next check in {args.interval} min... (Ctrl+C to stop)\n")
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print(f"\n\n{'='*70}")
            print(f"  POSITION MANAGER STOPPED")
            print(f"  State saved to {STATE_FILE}")
            print(f"  Exit log: {EXIT_LOG_FILE}")
            print(f"{'='*70}\n")
            break


def main():
    parser = argparse.ArgumentParser(
        description="Bybit Position Manager — adaptive trailing stops for max profit"
    )
    parser.add_argument("--live", action="store_true",
                        help="Enable LIVE stop management (default is dry-run)")
    parser.add_argument("--testnet", action="store_true", help="Use testnet")
    parser.add_argument("--interval", type=int, default=DEFAULT_CHECK_INTERVAL,
                        help=f"Check interval in minutes (default: {DEFAULT_CHECK_INTERVAL})")
    parser.add_argument("--initial-sl", type=float, default=DEFAULT_INITIAL_SL_PCT,
                        help=f"Initial stop loss %% (default: {DEFAULT_INITIAL_SL_PCT})")
    args = parser.parse_args()

    if args.live and not args.testnet:
        print(f"\n  WARNING: LIVE mode will modify stop losses on your REAL positions.")
        print(f"  Initial SL: {args.initial_sl}% | Trailing tiers active")
        confirm = input("\n  Type CONFIRM to proceed: ").strip()
        if confirm.upper() != "CONFIRM":
            print("  Cancelled.")
            sys.exit(0)

    run_manager(args)


if __name__ == "__main__":
    main()
