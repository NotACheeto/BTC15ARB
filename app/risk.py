"""Risk management, kill switch, position limits, and order deduplication engine.

Enforces fail-closed safety assertions prior to order routing.
"""

from decimal import Decimal
import logging
from typing import Literal

from app.config import Settings
from app.models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class RiskManager:
    """Central risk gateway and kill switch controller."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.kill_switch_active: bool = False
        self.kill_switch_reason: str | None = None

        # Mode state
        self.trading_mode: Literal["DATA", "PAPER", "LIVE"] = (
            "LIVE" if settings.LIVE_TRADING else ("PAPER" if settings.PAPER_TRADING else "DATA")
        )
        self.live_confirmed: bool = False

        # Exposure tracking
        self.active_arbitrages: dict[str, ArbitrageOpportunity] = {}
        self.active_market_windows: set[str] = set()
        self.current_unhedged_contracts: int = 0
        self.cumulative_unhedged_contracts: int = 0

        # Balances
        self.polymarket_balance: Decimal = Decimal("0.00")
        self.kalshi_balance: Decimal = Decimal("0.00")
        self.balances_verified: bool = False

    def activate_kill_switch(self, reason: str) -> None:
        """Instantly activate global kill switch and halt all execution."""
        self.kill_switch_active = True
        self.kill_switch_reason = reason
        logger.critical("EMERGENCY KILL SWITCH ACTIVATED: %s", reason)

    def deactivate_kill_switch(self) -> None:
        """Reset kill switch manually."""
        self.kill_switch_active = False
        self.kill_switch_reason = None
        logger.info("Kill switch deactivated manually")

    def enable_live_trading(self) -> bool:
        """Explicit authorization required to enable LIVE mode."""
        if not self.balances_verified and not (self.settings.POLYMARKET_US_KEY_ID and self.settings.KALSHI_API_KEY_ID):
            logger.warning("Cannot enable LIVE mode: credentials or balances not verified")
            return False
        self.trading_mode = "LIVE"
        self.live_confirmed = True
        logger.warning("LIVE TRADING MODE ENABLED. REAL CAPITAL AT RISK.")
        return True

    def disable_live_trading(self) -> None:
        """Revert to PAPER mode."""
        self.trading_mode = "PAPER"
        self.live_confirmed = False
        logger.info("Reverted to PAPER trading mode")

    def update_balances(self, poly_usd: Decimal, kalshi_usd: Decimal) -> None:
        """Update verified account balances."""
        self.polymarket_balance = poly_usd
        self.kalshi_balance = kalshi_usd
        self.balances_verified = True

    def validate_pre_execution(
        self,
        opp: ArbitrageOpportunity,
    ) -> tuple[bool, str]:
        """Perform comprehensive pre-trade risk and safety verification.
        
        Returns (is_approved, rejection_reason).
        """
        # 1. Kill Switch Check
        if self.kill_switch_active:
            return False, f"Global Kill Switch is active ({self.kill_switch_reason})"

        # 2. Execution Mode Verification
        if self.trading_mode == "DATA":
            return False, "Trading engine is in DATA read-only mode"

        if self.trading_mode == "LIVE" and not self.live_confirmed:
            return False, "LIVE trading selected but lacks explicit confirmation"

        # 3. Opportunity Executability Check
        if not opp.is_executable:
            return False, opp.rejection_reason or "Opportunity flagged non-executable"

        # 4. Strict Market Window & Opportunity Deduplication
        if opp.market_window in self.active_market_windows:
            return False, f"Active arbitrage already executing for window '{opp.market_window}'"

        if opp.opportunity_id in self.active_arbitrages:
            return False, f"Opportunity ID '{opp.opportunity_id}' is already executing"

        # 5. Position & Concurrency Limits
        if len(self.active_arbitrages) >= self.settings.MAX_CONCURRENT_ARBITRAGES:
            return False, (
                f"Concurrent arbitrage limit reached "
                f"({len(self.active_arbitrages)} >= {self.settings.MAX_CONCURRENT_ARBITRAGES})"
            )

        if opp.target_quantity > self.settings.MAX_CONTRACTS_PER_LEG:
            return False, (
                f"Order quantity {opp.target_quantity} exceeds MAX_CONTRACTS_PER_LEG "
                f"({self.settings.MAX_CONTRACTS_PER_LEG})"
            )

        if self.current_unhedged_contracts >= self.settings.MAX_UNHEDGED_POSITION:
            return False, (
                f"Unhedged position limit reached "
                f"({self.current_unhedged_contracts} >= {self.settings.MAX_UNHEDGED_POSITION})"
            )

        # 6. Capital / Balance Sufficiency (for LIVE mode)
        if self.trading_mode == "LIVE":
            if not self.balances_verified:
                return False, "Cannot execute live order: account balances not yet verified"

            # Check Polymarket capital
            poly_req = (
                (opp.executable_max_yes_price if opp.yes_venue.value == "POLYMARKET_US" else opp.executable_max_no_price)
                * Decimal(opp.target_quantity)
                + opp.poly_fee
            )
            if self.polymarket_balance < poly_req:
                return False, f"Insufficient Polymarket balance: ${self.polymarket_balance} < ${poly_req}"

            # Check Kalshi capital
            kalshi_req = (
                (opp.executable_max_yes_price if opp.yes_venue.value == "KALSHI" else opp.executable_max_no_price)
                * Decimal(opp.target_quantity)
                + opp.kalshi_fee
            )
            if self.kalshi_balance < kalshi_req:
                return False, f"Insufficient Kalshi balance: ${self.kalshi_balance} < ${kalshi_req}"

        # 7. Monotonic Expiration Guard
        if opp.polymarket_market.interval_end <= opp.detected_at:
            return False, "Market interval has expired"

        return True, "Approved"

    def record_arbitrage_started(self, opp: ArbitrageOpportunity) -> None:
        """Track active execution."""
        self.active_arbitrages[opp.opportunity_id] = opp
        self.active_market_windows.add(opp.market_window)

    def record_arbitrage_finished(
        self,
        opp_id: str,
        market_window: str,
        unhedged: int = 0,
        orphan_encountered: int = 0,
    ) -> None:
        """Release concurrency locks and record unhedged and cumulative orphan contracts."""
        self.active_arbitrages.pop(opp_id, None)
        self.active_market_windows.discard(market_window)
        self.current_unhedged_contracts += max(0, unhedged)
        self.cumulative_unhedged_contracts += max(0, orphan_encountered)
