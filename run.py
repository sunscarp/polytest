#!/usr/bin/env python3
"""
Weather NO Simulator Launcher
Starts the trading bot loop in a background thread and serves the dashboard.

Usage:
    python run.py              # bot + dashboard
    python run.py --dashboard  # dashboard only
    python run.py --bot        # bot only (no dashboard)
"""

import os
import sys
import threading
import webbrowser
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PORT = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", 8081)))


def start_bot():
    """Run the bot monitoring loop in a background thread."""
    from main import cmd_run
    print("  [bot] Starting trading bot loop...")
    try:
        cmd_run()
    except KeyboardInterrupt:
        print("\n  [bot] Stopped.")


def start_dashboard():
    """Run the dashboard server."""
    import dashboard
    print("  [dashboard] Starting dashboard server...")
    dashboard.main()


def open_browser(delay=2):
    time.sleep(delay)
    webbrowser.open(f"http://localhost:{PORT}")


def main():
    args = sys.argv[1:]
    dash_only = "--dashboard" in args
    bot_only = "--bot" in args

    print(f"\n{'='*50}")
    print(f"  NO TRADING SIMULATOR LAUNCHER")
    print(f"{'='*50}")

    if dash_only:
        print(f"  Mode: Dashboard only")
        print(f"{'='*50}\n")
        start_dashboard()
        return

    if bot_only:
        print(f"  Mode: Bot only")
        print(f"{'='*50}\n")
        start_bot()
        return

    # Both: bot in background, dashboard in foreground
    print(f"  Mode: Bot + Dashboard")
    print(f"  Dashboard: http://localhost:{PORT}")
    print(f"{'='*50}\n")

    bot_thread = threading.Thread(target=start_bot, daemon=True, name="bot")
    bot_thread.start()

    if not os.environ.get("PORT"):
        threading.Thread(target=open_browser, daemon=True, args=(3,)).start()

    try:
        start_dashboard()
    except KeyboardInterrupt:
        print("\n  Stopping...")
        sys.exit(0)


if __name__ == "__main__":
    main()
