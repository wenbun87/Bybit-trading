#!/bin/bash
# Fallback launcher — double-click in Finder (opens a small terminal window).
# Prefer "Trading Dashboard.app" for a fully terminal-free experience.
cd "$(dirname "$0")" || exit 1

# Activate venv if it exists
for venv in "$HOME/trading-venv" ./venv ./.venv; do
    [ -f "$venv/bin/activate" ] && source "$venv/bin/activate" && break
done

python3 run_dashboard.py
