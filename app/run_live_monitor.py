"""Run the durable live monitor on a host with continuous network access.

The strategy scheduler can run independently; both commands share the database
lock. This process never performs entry scans. It inherits environment secrets
without writing credentials into command arguments or logs.
"""

import logging
import subprocess
import sys
import time

from app.core.config import settings


def main():
    logging.basicConfig(level=logging.INFO)
    if not settings.LIVE_TRADING_ENABLED:
        raise SystemExit("Set LIVE_TRADING_ENABLED=true only after read-only and testnet checks")
    interval = settings.LIVE_MONITOR_INTERVAL_SECONDS
    if interval < 5:
        raise SystemExit("LIVE_MONITOR_INTERVAL_SECONDS must be at least 5")
    while True:
        started = time.monotonic()
        result = subprocess.run([
            sys.executable, "-m", "app.run_cycle", "--trade-mode", "live", "--monitor-only",
        ], check=False)
        if result.returncode:
            logging.error("Live monitor failed: inspect logs and exchange orders; new entries must remain paused until resolved")
        time.sleep(max(1, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
