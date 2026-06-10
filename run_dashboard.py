#!/usr/bin/env python3
"""
One-command launcher for the Trading Bot Dashboard.

Usage:
    python3 run_dashboard.py

Opens a local web dashboard at http://127.0.0.1:8420
If the dashboard is already running, just opens the browser.
"""
import os
import socket
import sys
import threading
import time
import webbrowser

PORT = 8420
URL = f"http://127.0.0.1:{PORT}"


def check_deps():
    """Check that fastapi and uvicorn are installed."""
    missing = []
    try:
        import fastapi  # noqa: F401
    except ImportError:
        missing.append("fastapi")
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        missing.append("uvicorn")

    if missing:
        print(f"\n  Missing dependencies: {', '.join(missing)}")
        print(f"  Install with:\n")
        print(f"    pip3 install fastapi uvicorn\n")
        sys.exit(1)


def already_running() -> bool:
    """True if something is already listening on the dashboard port."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def main():
    # Run from the repo directory so bots and state files resolve correctly
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if already_running():
        print(f"\n  Dashboard already running at {URL} — opening browser.\n")
        webbrowser.open(URL)
        return

    check_deps()

    import uvicorn

    print(f"\n  Trading Bot Dashboard")
    print(f"  {'─' * 40}")
    print(f"  URL: {URL}")
    print(f"  Press Ctrl+C to stop\n")

    # Open browser after a short delay
    def open_browser():
        time.sleep(1.5)
        webbrowser.open(URL)

    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(
        "dashboard.app:app",
        host="127.0.0.1",
        port=PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
