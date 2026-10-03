"""Main application orchestrator for Polymarket US - Kalshi BTC Arbitrage Bot.

Follows the strict startup sequence, manages the async event loop,
coordinates feeds, market discovery, arbitrage evaluation, execution, and dashboard server.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import logging
import sys
import uvicorn

from app.arbitrage import ArbitrageCalculator
from app.config import Settings, get_settings
from app.dashboard.api import create_dashboard_app
from app.dashboard.state import dashboard_state
from app.exchanges.kalshi import KalshiClient
from app.exchanges.polymarket_us import PolymarketUSClient
from app.execution import ExecutionEngine
from app.feeds.kalshi_ws import KalshiFeed
from app.feeds.polymarket_ws import PolymarketFeed
from app.latency import latency_tracker
from app.market_discovery import MarketDiscoveryEngine
from app.models import OrderBookState
from app.persistence import DatabaseManager
from app.portfolio import PortfolioManager
from app.risk import RiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("BTC15ArbBot")


class TradingApplication:
    """Core arbitrage bot application coordinator."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.db = DatabaseManager(self.settings.DB_PATH)
        self.risk_manager = RiskManager(self.settings)

        # Exchange clients
        self.poly_client = PolymarketUSClient(self.settings)
        self.kalshi_client = KalshiClient(self.settings)

        # Portfolio & Telemetry
        self.portfolio_manager = PortfolioManager(self.poly_client, self.kalshi_client)

        # Market Discovery & Feeds
        self.discovery_engine = MarketDiscoveryEngine(self.settings)
        self.poly_feed = PolymarketFeed(self.settings, self.poly_client)
        self.kalshi_feed = KalshiFeed(self.settings, self.kalshi_client)

        # Arbitrage & Execution Engines
        self.arbitrage_calculator = ArbitrageCalculator(self.settings)
        self.execution_engine = ExecutionEngine(
            self.settings, self.risk_manager, self.poly_client, self.kalshi_client
        )

        self._running = False
        self._discovery_task: asyncio.Task | None = None
        self._dashboard_server: uvicorn.Server | None = None
        self._active_market_pair = None

    async def startup(self) -> None:
        """Execute ordered startup sequence."""
        logger.info("================================================================")
        logger.info("STARTING BTC 15-MINUTE CROSS-EXCHANGE ARBITRAGE SYSTEM")
        logger.info("================================================================")

        # 1 & 2: Configuration & Secrets check
        logger.info("Step 1: Configuration loaded (LIVE_TRADING=%s, PAPER_TRADING=%s)",
                    self.settings.LIVE_TRADING, self.settings.PAPER_TRADING)
        logger.info("Polymarket credentials present: %s", self.poly_client.is_authenticated())
        logger.info("Kalshi credentials present: %s", self.kalshi_client.is_authenticated())

        dashboard_state.polymarket_connected = self.poly_client.is_authenticated()
        dashboard_state.kalshi_connected = self.kalshi_client.is_authenticated()
        dashboard_state.trading_mode = self.risk_manager.trading_mode

        # 3 & 4: Database initialized in constructor
        await self.db.log_system_event("STARTUP", "Arbitrage bot initialized")

        # 5: Register feed callbacks
        self.poly_feed.on_update(self._on_market_data_update)
        self.kalshi_feed.on_update(self._on_market_data_update)

        # 6: Initial Market Discovery & Validation
        logger.info("Step 2: Discovering active BTC 15-minute market contracts...")
        active_pair, upcoming = await self.discovery_engine.update_active_and_upcoming_pairs()

        if active_pair:
            p_mkt, k_mkt, res = active_pair
            self._active_market_pair = (p_mkt, k_mkt)
            dashboard_state.current_poly_market = p_mkt
            dashboard_state.current_kalshi_market = k_mkt
            dashboard_state.equivalence_result = res
            await self.db.log_equivalence_result(res)

            logger.info("Step 3: Market Equivalence Status: %s - %s", res.status, res.reason)

            # Start streaming market data for active pair
            await self.poly_feed.start(p_mkt.ticker)
            await self.kalshi_feed.start(k_mkt.ticker)
        else:
            logger.warning("No active verified BTC 15m pair found on startup. Waiting for discovery loop...")

        # 7: Balances & Portfolio check
        if self.poly_client.is_authenticated() and self.kalshi_client.is_authenticated():
            p_bal, k_bal = await self.portfolio_manager.refresh_balances()
            self.risk_manager.update_balances(p_bal, k_bal)
            logger.info("Verified Balances: Poly=$%.2f, Kalshi=$%.2f", p_bal, k_bal)
        dashboard_state.portfolio_summary = self.portfolio_manager.get_portfolio_summary()

        # 8: Start Dashboard Web Server
        logger.info("Step 4: Starting Local Web Dashboard at http://%s:%d",
                    self.settings.DASHBOARD_HOST, self.settings.DASHBOARD_PORT)
        app = create_dashboard_app(self.risk_manager)
        config = uvicorn.Config(
            app=app,
            host=self.settings.DASHBOARD_HOST,
            port=self.settings.DASHBOARD_PORT,
            log_level="warning",
            loop="asyncio",
        )
        self._dashboard_server = uvicorn.Server(config)
        asyncio.create_task(self._dashboard_server.serve())

        # 9: Start background discovery & rollover monitor
        self._running = True
        dashboard_state.is_running = True
        self._discovery_task = asyncio.create_task(self._discovery_loop())

        logger.info("Step 5: System Operational. Monitoring BTC 15m markets in %s mode.",
                    self.risk_manager.trading_mode)

    async def shutdown(self) -> None:
        """Graceful teardown of connections and sessions."""
        logger.info("Initiating graceful shutdown...")
        self._running = False
        dashboard_state.is_running = False

        if self._discovery_task:
            self._discovery_task.cancel()

        await self.poly_feed.stop()
        await self.kalshi_feed.stop()
        await self.poly_client.close()
        await self.kalshi_client.close()
        await self.discovery_engine.close()

        if self._dashboard_server:
            self._dashboard_server.should_exit = True

        logger.info("Shutdown complete.")

    async def _discovery_loop(self) -> None:
        """Continuously discover upcoming intervals and handle market rollover."""
        while self._running:
            try:
                await asyncio.sleep(10.0)
                active_pair, upcoming = await self.discovery_engine.update_active_and_upcoming_pairs()

                if active_pair:
                    p_mkt, k_mkt, res = active_pair
                    dashboard_state.current_poly_market = p_mkt
                    dashboard_state.current_kalshi_market = k_mkt
                    dashboard_state.equivalence_result = res

                    # If active pairing changed (rollover occurred), switch feeds
                    if not self._active_market_pair or (self._active_market_pair[0].ticker != p_mkt.ticker):
                        logger.info("Market rollover detected: Switching to '%s' <-> '%s'",
                                    p_mkt.ticker, k_mkt.ticker)
                        self._active_market_pair = (p_mkt, k_mkt)
                        await self.poly_feed.stop()
                        await self.kalshi_feed.stop()
                        await self.poly_feed.start(p_mkt.ticker)
                        await self.kalshi_feed.start(k_mkt.ticker)
                else:
                    if self._active_market_pair:
                        logger.info("Active interval has closed. Pausing feeds until next interval opens...")
                        self._active_market_pair = None
                        await self.poly_feed.stop()
                        await self.kalshi_feed.stop()
                        from app.models import MarketEquivalenceResult
                        dashboard_state.equivalence_result = MarketEquivalenceResult(
                            is_equivalent=False,
                            status="REJECTED",
                            reason="Current 15-minute interval has closed; waiting for next interval discovery"
                        )

                if self.poly_client.is_authenticated() and self.kalshi_client.is_authenticated():
                    p_bal, k_bal = await self.portfolio_manager.refresh_balances()
                    self.risk_manager.update_balances(p_bal, k_bal)

                dashboard_state.latency_summary = latency_tracker.get_summary()
                dashboard_state.portfolio_summary = self.portfolio_manager.get_portfolio_summary()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in market discovery loop: %s", e)

    def _on_market_data_update(self, book: OrderBookState) -> None:
        """Handle real-time quote arrival on hot path."""
        # Update dashboard state
        if book.venue.value == "POLYMARKET_US":
            dashboard_state.poly_book = book
        else:
            dashboard_state.kalshi_book = book

        # Need both books and verified market pair to evaluate arbitrage
        if not self._active_market_pair or not dashboard_state.poly_book or not dashboard_state.kalshi_book:
            return

        poly_mkt, kalshi_mkt = self._active_market_pair
        poly_b = dashboard_state.poly_book
        kalshi_b = dashboard_state.kalshi_book

        # Evaluate both directions
        opps = self.arbitrage_calculator.evaluate_opportunity(
            poly_market=poly_mkt,
            kalshi_market=kalshi_mkt,
            poly_book=poly_b,
            kalshi_book=kalshi_b,
        )
        dashboard_state.latest_opportunities = opps

        # Hot path evaluation
        for opp in opps:
            if opp.is_executable and opp.conservative_net_edge >= self.settings.MIN_NET_EDGE:
                is_approved, reason = self.risk_manager.validate_pre_execution(opp)
                if is_approved:
                    logger.info(
                        "EXECUTABLE ARBITRAGE TRIGGERED: %s | Gross=$%.4f, Net=$%.4f",
                        opp.direction, opp.theoretical_gross_edge, opp.conservative_net_edge
                    )
                    asyncio.create_task(self._execute_opp(opp))
                else:
                    opp.is_executable = False
                    opp.rejection_reason = reason
                    dashboard_state.recent_rejections.append({
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                        "direction": opp.direction,
                        "theoretical_gross_edge": str(opp.theoretical_gross_edge),
                        "conservative_net_edge": str(opp.conservative_net_edge),
                        "rejection_reason": reason,
                    })
                    asyncio.create_task(self.db.log_rejected_opportunity(opp))

    async def _execute_opp(self, opp) -> None:
        """Execute approved arbitrage opportunity."""
        trade_record = await self.execution_engine.execute_arbitrage(opp)
        if trade_record:
            self.portfolio_manager.record_trade(trade_record)
            dashboard_state.recent_trades.append(trade_record)
            if self.poly_client.is_authenticated() and self.kalshi_client.is_authenticated():
                p_bal, k_bal = await self.portfolio_manager.refresh_balances()
                self.risk_manager.update_balances(p_bal, k_bal)
            dashboard_state.portfolio_summary = self.portfolio_manager.get_portfolio_summary()
            await self.db.log_trade(trade_record)
