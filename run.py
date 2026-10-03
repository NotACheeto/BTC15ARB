"""Root execution entrypoint for Polymarket US - Kalshi BTC Arbitrage Bot.

Usage:
    python run.py             # Runs in PAPER mode by default (safe)
    python run.py --data      # Runs in DATA-only mode (read-only)
    python run.py --live      # Requires confirmation and verified credentials
"""

import argparse
import asyncio
import logging
import signal
import sys

from app.config import get_settings
from app.main import TradingApplication

logger = logging.getLogger("BTC15ArbBot.Launcher")


def parse_args():
    parser = argparse.ArgumentParser(description="Polymarket US - Kalshi BTC 15M Arbitrage Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading mode (Safety default is False)")
    parser.add_argument("--data", action="store_true", help="Run in read-only data mode")
    parser.add_argument("--paper", action="store_true", default=True, help="Run in realistic paper simulation mode (Default)")
    parser.add_argument("--port", type=int, default=8050, help="Dashboard port (default: 8050)")
    return parser.parse_args()


async def main():
    args = parse_args()
    settings = get_settings()

    if args.port:
        settings.DASHBOARD_PORT = args.port

    if args.data:
        settings.DATA_MODE = True
        settings.PAPER_TRADING = False
        settings.LIVE_TRADING = False
    elif args.live:
        print("\n=======================================================")
        print("ATTENTION: YOU REQUESTED --live TRADING MODE.")
        print("REAL ORDERS WILL BE ROUTED IF CREDENTIALS PASS PREFLIGHT.")
        print("=======================================================\n")
        settings.LIVE_TRADING = True
        settings.PAPER_TRADING = False

    app = TradingApplication(settings)

    # Signal handlers
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _sig_handler():
        logger.info("Signal received, stopping...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except NotImplementedError:
            # Windows signal handler support
            pass

    try:
        await app.startup()
        while not stop_event.is_set():
            await asyncio.sleep(1.0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete.")
