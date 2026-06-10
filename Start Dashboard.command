#!/bin/bash
# Fallback launcher — double-click in Finder (opens a small terminal window).
# Prefer "Trading Dashboard.app" for a fully terminal-free experience.
cd "$(dirname "$0")" || exit 1
python3 run_dashboard.py
