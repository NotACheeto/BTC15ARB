"""Portfolio, position, and balance reconciliation service.

Maintains unified accounting across Polymarket US and Kalshi.
Tracks hedged vs unhedged positions, exposure, and realized/unrealized P&L.
"""

from decimal import Decimal
import logging
from app.exchanges.kalshi import KalshiClient
from app.exchanges.polymarket_us import PolymarketUSClient
from app.models import ArbitrageTradeRecord

logger = logging.getLogger(__name__)


class PortfolioManager:
    """Unified cross-venue portfolio and position tracker."""

    def __init__(self, poly_client: PolymarketUSClient, kalshi_client: KalshiClient):
        self.poly_client = poly_client
        self.kalshi_client = kalshi_client

        # Cached balances
        self.polymarket_cash_usd: Decimal = Decimal("0.00")
        self.kalshi_cash_usd: Decimal = Decimal("0.00")

        # Open positions: {market_ticker: {"yes": int, "no": int}}
        self.polymarket_positions: dict[str, dict[str, int]] = {}
        self.kalshi_positions: dict[str, dict[str, int]] = {}

        # Exposure & PnL
        self.paired_hedged_contracts: int = 0
        self.unhedged_contracts: int = 0
        self.cumulative_unhedged_contracts: int = 0
        self.realized_pnl: Decimal = Decimal("0.00")
        self.unrealized_pnl: Decimal = Decimal("0.00")
        self.trade_history: list[ArbitrageTradeRecord] = []

    async def refresh_balances(self) -> tuple[Decimal, Decimal]:
        """Fetch real balances from both venues if authenticated."""
        if self.poly_client.is_authenticated():
            try:
                resp = await self.poly_client.get_account_balances()
                # Parse cash balance
                balances = resp.get("balances", resp)
                if isinstance(balances, dict):
                    cash_val = balances.get("cash", {}).get("value", "0.00")
                    self.polymarket_cash_usd = Decimal(str(cash_val))
            except Exception as e:
                logger.warning("Error fetching Polymarket balances: %s", e)

        if self.kalshi_client.is_authenticated():
            try:
                resp = await self.kalshi_client.get_balance()
                # Kalshi returns balance in cents
                balance_cents = resp.get("balance", 0)
                self.kalshi_cash_usd = Decimal(str(balance_cents)) / Decimal("100")
            except Exception as e:
                logger.warning("Error fetching Kalshi balances: %s", e)

        return self.polymarket_cash_usd, self.kalshi_cash_usd

    def record_trade(self, trade: ArbitrageTradeRecord) -> None:
        """Record completed trade and update cumulative P&L and contract accounting."""
        from app.models import ExecutionStatus
        self.trade_history.append(trade)
        self.realized_pnl += trade.realized_pnl

        yes_qty = trade.leg_yes.fill_quantity if trade.leg_yes else 0
        no_qty = trade.leg_no.fill_quantity if trade.leg_no else 0
        hedged_qty = min(yes_qty, no_qty)
        self.paired_hedged_contracts += hedged_qty

        orphan_qty = abs(yes_qty - no_qty)
        if orphan_qty > 0:
            self.cumulative_unhedged_contracts += orphan_qty
            liquidated_qty = (
                trade.recovery_result.fill_quantity
                if (trade.recovery_result and trade.recovery_result.status == ExecutionStatus.FILLED)
                else 0
            )
            unhedged_remaining = max(0, orphan_qty - liquidated_qty)
            self.unhedged_contracts += unhedged_remaining

    def get_portfolio_summary(self) -> dict:
        """Snapshot of portfolio status for dashboard and risk checks."""
        return {
            "polymarket_balance_usd": float(self.polymarket_cash_usd),
            "kalshi_balance_usd": float(self.kalshi_cash_usd),
            "total_balance_usd": float(self.polymarket_cash_usd + self.kalshi_cash_usd),
            "paired_hedged_contracts": self.paired_hedged_contracts,
            "unhedged_contracts": self.unhedged_contracts,
            "cumulative_unhedged_contracts": self.cumulative_unhedged_contracts,
            "realized_pnl_usd": float(self.realized_pnl),
            "unrealized_pnl_usd": float(self.unrealized_pnl),
            "total_trades_count": len(self.trade_history),
        }
