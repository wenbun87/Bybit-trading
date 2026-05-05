"""
FastAPI dashboard for managing trading bots.
Run with: python3 run_dashboard.py
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from queue import Empty

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import bot_manager

app = FastAPI(title="Trading Bot Dashboard")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (TEMPLATES_DIR / "index.html").read_text()


@app.get("/api/status")
async def status():
    return bot_manager.get_all_status()


@app.post("/api/bot/{name}/start")
async def start_bot(name: str, request: Request):
    body = await request.json()
    amount = float(body.get("amount", 250))
    live = bool(body.get("live", False))
    extra_args = body.get("extra_args", [])
    result = bot_manager.start_bot(name, amount=amount, live=live,
                                   extra_args=extra_args)
    return result


@app.post("/api/bot/{name}/stop")
async def stop_bot(name: str):
    return bot_manager.stop_bot(name)


@app.post("/api/bot/{name}/caffeinate")
async def toggle_caffeinate(name: str):
    return bot_manager.toggle_caffeinate(name)


@app.get("/api/bot/{name}/logs")
async def stream_logs(name: str):
    """Server-Sent Events stream of bot logs."""
    q, buffer = bot_manager.subscribe_logs(name)

    async def event_generator():
        # Send buffered history
        for line in list(buffer):
            yield f"data: {line}\n\n"

        # Stream new lines
        try:
            while True:
                try:
                    line = q.get_nowait()
                    yield f"data: {line}\n\n"
                except Empty:
                    await asyncio.sleep(0.3)
                    # Send keepalive
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            bot_manager.unsubscribe_logs(name, q)
            raise
        finally:
            bot_manager.unsubscribe_logs(name, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
