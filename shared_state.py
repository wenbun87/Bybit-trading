"""Shared state for position/trade data between bots and dashboard."""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
POSITIONS_FILE = DATA_DIR / "positions.json"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.json"
RUNS_FILE = DATA_DIR / "runs.json"

def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def _atomic_write(path: Path, data):
    """Write JSON atomically (write tmp, then rename)."""
    _ensure_dir()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ── Runs ──

def _read_runs() -> dict:
    if not RUNS_FILE.exists():
        return {}
    try:
        with open(RUNS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def start_run(bot_name: str, live: bool = False) -> int:
    """Increment run counter for a bot, return new run id.
    Clears positions for paper mode only — live positions stay."""
    runs = _read_runs()
    bot_runs = runs.get(bot_name, {"current": 0, "history": []})
    bot_runs["current"] = bot_runs.get("current", 0) + 1
    bot_runs["history"].append({
        "run": bot_runs["current"],
        "started_at": time.time(),
        "live": live,
    })
    runs[bot_name] = bot_runs
    _atomic_write(RUNS_FILE, runs)
    if not live:
        clear_positions(bot_name)
    return bot_runs["current"]

def get_current_run(bot_name: str) -> int:
    runs = _read_runs()
    return runs.get(bot_name, {}).get("current", 0)


# ── Positions ──

def write_positions(bot_name: str, positions: list[dict]):
    """Write current positions for a bot. Called each scan cycle."""
    all_positions = read_all_positions()
    all_positions[bot_name] = {
        "positions": positions,
        "updated_at": time.time(),
    }
    _atomic_write(POSITIONS_FILE, all_positions)

def read_all_positions() -> dict:
    """Read positions from all bots."""
    if not POSITIONS_FILE.exists():
        return {}
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def clear_positions(bot_name: str):
    """Remove all positions for a bot (called on stop)."""
    all_positions = read_all_positions()
    if bot_name in all_positions:
        del all_positions[bot_name]
        _atomic_write(POSITIONS_FILE, all_positions)


# ── Trades ──

def append_trade(bot_name: str, trade: dict):
    """Append a closed trade to history, tagged with current run."""
    history = read_trade_history()
    trade["bot"] = bot_name
    trade["closed_at"] = time.time()
    trade["run"] = get_current_run(bot_name)
    history.append(trade)
    if len(history) > 500:
        history = history[-500:]
    _atomic_write(TRADE_HISTORY_FILE, history)

def read_trade_history(limit: int = 100) -> list[dict]:
    """Read trade history."""
    if not TRADE_HISTORY_FILE.exists():
        return []
    try:
        with open(TRADE_HISTORY_FILE) as f:
            data = json.load(f)
        return data[-limit:] if limit else data
    except (json.JSONDecodeError, OSError):
        return []

def get_stats() -> dict:
    """Compute aggregate stats from trade history."""
    trades = read_trade_history(limit=0)
    if not trades:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl_usd": 0, "total_pnl_pct": 0, "avg_hold_hours": 0}

    wins = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
    losses = sum(1 for t in trades if t.get("pnl_pct", 0) <= 0)
    total_pnl_usd = sum(t.get("pnl_usd", 0) for t in trades)
    total_pnl_pct = sum(t.get("pnl_pct", 0) for t in trades)

    hold_times = []
    for t in trades:
        if t.get("entry_time") and t.get("closed_at"):
            hold_times.append((t["closed_at"] - t["entry_time"]) / 3600)
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) * 100 if trades else 0,
        "total_pnl_usd": round(total_pnl_usd, 2),
        "avg_pnl_pct": round(total_pnl_pct / len(trades), 2) if trades else 0,
        "avg_hold_hours": round(avg_hold, 1),
    }
