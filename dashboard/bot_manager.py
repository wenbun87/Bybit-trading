"""
Bot process manager — spawns, stops, and streams logs from trading bots.
Handles caffeinate integration on macOS.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue, Empty

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))
import shared_state

BOTS = {
    "auto_trader": {
        "script": "auto_trader.py",
        "label": "Accumulation Trader",
        "default_args": [],
    },
    "sfp_scanner": {
        "script": "sfp_scanner.py",
        "label": "SFP Scanner",
        "default_args": ["--watch", "5"],
    },
}


@dataclass
class BotState:
    name: str
    process: subprocess.Popen | None = None
    caffeinate_proc: subprocess.Popen | None = None
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=1000))
    subscribers: list = field(default_factory=list)
    status: str = "stopped"
    caffeinate_on: bool = False
    is_live: bool = False
    started_at: float | None = None
    args_used: list = field(default_factory=list)


_bots: dict[str, BotState] = {
    "auto_trader": BotState(name="auto_trader"),
    "sfp_scanner": BotState(name="sfp_scanner"),
}


def get_state(name: str) -> BotState:
    return _bots[name]


def get_all_status() -> list[dict]:
    result = []
    for name, state in _bots.items():
        info = BOTS[name]
        result.append({
            "name": name,
            "label": info["label"],
            "status": state.status,
            "caffeinate": state.caffeinate_on,
            "pid": state.process.pid if state.process else None,
            "started_at": state.started_at,
            "uptime_s": (time.time() - state.started_at) if state.started_at else 0,
        })
    return result


def _reader_thread(state: BotState):
    """Read stdout from bot process line by line, broadcast to subscribers."""
    proc = state.process
    if not proc or not proc.stdout:
        return
    for line in iter(proc.stdout.readline, ""):
        if not line:
            break
        line = line.rstrip("\n")
        state.log_buffer.append(line)
        for q in list(state.subscribers):
            try:
                q.put_nowait(line)
            except Exception:
                pass
    proc.wait()
    state.status = "stopped"
    state.started_at = None
    if not state.is_live:
        shared_state.clear_positions(state.name)
    state.log_buffer.append(f"[BOT EXITED with code {proc.returncode}]")
    for q in list(state.subscribers):
        try:
            q.put_nowait(f"[BOT EXITED with code {proc.returncode}]")
        except Exception:
            pass
    _stop_caffeinate(state)


def start_bot(name: str, amount: float = 250, live: bool = False,
              account_balance: float = 500, max_exposure_mult: float = 5,
              extra_args: list | None = None) -> dict:
    state = _bots[name]
    if state.status == "running":
        return {"ok": False, "error": "already running"}

    info = BOTS[name]
    cmd = ["python3", "-u", str(BOT_DIR / info["script"])]

    cmd.extend(["--amount", str(amount)])
    cmd.extend(["--account-balance", str(account_balance),
                "--max-exposure-mult", str(max_exposure_mult)])
    cmd.extend(info["default_args"])

    if live:
        cmd.append("--live")
        cmd.append("--no-confirm")
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(BOT_DIR),
            env=env,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    state.process = proc
    state.status = "running"
    state.is_live = live
    state.started_at = time.time()
    state.args_used = cmd[2:]  # skip python3 -u
    state.log_buffer.clear()

    run_id = shared_state.start_run(name, live=live)
    state.log_buffer.append(f"[STARTED] Run #{run_id} | {' '.join(cmd)}")

    # Start log reader thread
    t = threading.Thread(target=_reader_thread, args=(state,), daemon=True)
    t.start()

    # Caffeinate if toggled on
    if state.caffeinate_on:
        _start_caffeinate(state)

    return {"ok": True, "pid": proc.pid}


def stop_bot(name: str) -> dict:
    state = _bots[name]
    if state.status != "running" or not state.process:
        return {"ok": False, "error": "not running"}

    proc = state.process
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)

    state.status = "stopped"
    state.started_at = None
    if state.is_live:
        state.log_buffer.append("[STOPPED by user — live positions kept on exchange]")
    else:
        state.log_buffer.append("[STOPPED by user]")
        shared_state.clear_positions(name)
    _stop_caffeinate(state)
    return {"ok": True}


def _start_caffeinate(state: BotState):
    if not state.process or state.caffeinate_proc:
        return
    try:
        state.caffeinate_proc = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(state.process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        state.log_buffer.append("[WARN] caffeinate not found (not macOS?)")


def _stop_caffeinate(state: BotState):
    if state.caffeinate_proc:
        try:
            state.caffeinate_proc.terminate()
        except Exception:
            pass
        state.caffeinate_proc = None


def toggle_caffeinate(name: str) -> dict:
    state = _bots[name]
    state.caffeinate_on = not state.caffeinate_on

    if state.caffeinate_on and state.status == "running":
        _start_caffeinate(state)
    elif not state.caffeinate_on:
        _stop_caffeinate(state)

    return {"ok": True, "caffeinate": state.caffeinate_on}


def subscribe_logs(name: str) -> tuple[Queue, deque]:
    state = _bots[name]
    q: Queue = Queue(maxsize=200)
    state.subscribers.append(q)
    return q, state.log_buffer


def unsubscribe_logs(name: str, q: Queue):
    state = _bots[name]
    if q in state.subscribers:
        state.subscribers.remove(q)
