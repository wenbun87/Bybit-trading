#!/usr/bin/env python3
"""
One-command launcher for the Trading Bot Dashboard.

Usage:
    python3 run_dashboard.py

Opens a local web dashboard at http://127.0.0.1:8420
"""
import subprocess
import sys
import webbrowser
import time


def check_deps():
    """Check that fastapi and uvicorn are installed."""
    missing = []
    try:
        import fastapi
    except ImportError:
        missing.append("fastapi")
    try:
        import uvicorn
    except ImportError:
        missing.append("uvicorn")

    if missing:
        print(f"\n  Missing dependencies: {', '.join(missing)}")
        print(f"  Install with:\n")
        print(f"    pip3 install fastapi uvicorn\n")
        sys.exit(1)


def main():
    check_deps()

    import uvicorn

    port = 8420
    url = f"http://127.0.0.1:{port}"

    print(f"\n  Trading Bot Dashboard")
    print(f"  {'─' * 40}")
    print(f"  URL: {url}")
    print(f"  Press Ctrl+C to stop\n")

    # Open browser after a short delay
    def open_browser():
        time.sleep(1.5)
        webbrowser.open(url)

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(
        "dashboard.app:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
