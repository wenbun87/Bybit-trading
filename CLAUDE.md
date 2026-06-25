# Bybit Trading Bot

## Project Overview
Two automated Bybit USDT perpetual futures bots with a web dashboard:
- **Accumulation/Momentum Bot** (`auto_trader.py`) — finds quiet Pool D coins showing early accumulation, enters LONG before the pump
- **SFP Scanner** (`sfp_scanner.py`) — trades Swing Failure Patterns with graded entries (A+ to D)

## Key Files
- `auto_trader.py` — Main momentum bot. Entry scoring, exit strategy, paper/live trading
- `momentum_scanner.py` — Signal analysis: accumulation, pre-squeeze, volume, OI, funding, crime pump detection
- `sfp_scanner.py` — SFP pattern scanner with trailing stops
- `shared_state.py` — Shared data layer (positions, trade history, stats)
- `run_dashboard.py` — Dashboard launcher (port 8420)
- `dashboard/app.py` — FastAPI dashboard backend
- `dashboard/templates/index.html` — Dashboard UI (user may swap between dark/white themes locally)
- `STRATEGY.md` / `Momentum Bot Strategy.pdf` — Full strategy documentation

## Current Branch
`claude/bybit-trading-integration-J2OWh` — all work is on this branch.

## Strategy Summary
- **Entry**: Pool D only (<$5M turnover), score 40+ from weighted signals (accumulation 30%, pre-squeeze 20%, volume/OI/funding/streak 10% each). Crime score >= 60 blocks entry, 30-60 halves size.
- **Exit layers**: Hard stop -8% → Scale-out 50% at +25% → Runner exits (OI divergence 20% drop, funding decay <30% of peak, structure break below higher low, ratchet floors) → Signal-based exits (graduation, funding positive, OI dropping)
- **Sizing**: Score-based tiers (0.75x to 3x base margin), 5x leverage default

## Data Storage
- `data/trade_history.json` — Closed trades (dashboard reads this)
- `data/positions.json` — Open positions
- `data/runs.json` — Run metadata
- `data/auto_trader_state.json` — Momentum bot session state
- `data/sfp_state.json` — SFP bot session state
- `trade_log.csv` — Raw CSV trade log
- Reset all: `python3 -c "import shared_state; shared_state.reset_all_data()"`

## Running
```bash
# Dashboard
python3 run_dashboard.py  # http://localhost:8420

# Momentum bot (paper)
python3 auto_trader.py

# Momentum bot (live)
python3 auto_trader.py --live --amount 250

# SFP bot (paper)
python3 sfp_scanner.py
```

## Environment
- Python 3 with venv at `~/trading-venv`
- Requires: `BYBIT_API_KEY` and `BYBIT_API_SECRET` env vars for live mode
- Dependencies: fastapi, uvicorn (for dashboard)

## Recent Changes (this session)
1. Added funding normalization exit for runners (detect squeeze fuel exhaustion)
2. Unified strategy — removed lottery/regular split, crime score modifies size
3. Scale-out + runner exit strategy (OI divergence, structure break, ratchet floors)
4. Tightened SFP trailing stops, added 24h stale exit
5. Dashboard no-cache headers fix
6. Created STRATEGY.md and PDF for trader review

## Pending Ideas
- Telegram bot integration for mobile control (start/stop bots, notifications, P&L)
- Backtest framework for exit strategy replay
- Loop engineering: run paper mode, analyze results, tune parameters iteratively
