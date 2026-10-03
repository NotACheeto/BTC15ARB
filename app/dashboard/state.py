"""Shared thread-safe in-memory state for dashboard presentation."""

from datetime import datetime, timezone
from typing import Any
from app.models import (
    ArbitrageOpportunity,
    ArbitrageTradeRecord,
    MarketEquivalenceResult,
    NormalizedMarket,
    OrderBookState,
)


class DashboardState:
    """Snapshot store consumed by FastAPI dashboard endpoints."""

    def __init__(self):
        # System
        self.is_running: bool = False
        self.trading_mode: str = "PAPER"
        self.kill_switch_active: bool = False
        self.kill_switch_reason: str | None = None
        self.polymarket_connected: bool = False
        self.kalshi_connected: bool = False
        self.last_heartbeat: datetime = datetime.now(timezone.utc)

        # Active Markets & Matching
        self.current_poly_market: NormalizedMarket | None = None
        self.current_kalshi_market: NormalizedMarket | None = None
        self.equivalence_result: MarketEquivalenceResult | None = None

        # Live Order Books
        self.poly_book: OrderBookState | None = None
        self.kalshi_book: OrderBookState | None = None

        # Opportunities
        self.latest_opportunities: list[ArbitrageOpportunity] = []
        self.recent_trades: list[ArbitrageTradeRecord] = []
        self.recent_rejections: list[dict[str, Any]] = []

        # Portfolio & Telemetry
        self.portfolio_summary: dict[str, Any] = {}
        self.latency_summary: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for frontend JSON payload."""
        poly_mkt_dict = self.current_poly_market.model_dump() if self.current_poly_market else None
        kalshi_mkt_dict = self.current_kalshi_market.model_dump() if self.current_kalshi_market else None

        poly_b = None
        if self.poly_book:
            poly_b = {
                "best_yes_bid": float(self.poly_book.best_yes_bid) if self.poly_book.best_yes_bid else None,
                "best_yes_ask": float(self.poly_book.best_yes_ask) if self.poly_book.best_yes_ask else None,
                "best_no_bid": float(self.poly_book.best_no_bid) if self.poly_book.best_no_bid else None,
                "best_no_ask": float(self.poly_book.best_no_ask) if self.poly_book.best_no_ask else None,
                "best_yes_ask_size": float(self.poly_book.best_yes_ask_size) if self.poly_book.best_yes_ask_size else None,
                "timestamp_ns": self.poly_book.timestamp_ns,
                "updated_at": self.poly_book.updated_at.isoformat(),
            }

        kalshi_b = None
        if self.kalshi_book:
            kalshi_b = {
                "best_yes_bid": float(self.kalshi_book.best_yes_bid) if self.kalshi_book.best_yes_bid else None,
                "best_yes_ask": float(self.kalshi_book.best_yes_ask) if self.kalshi_book.best_yes_ask else None,
                "best_no_bid": float(self.kalshi_book.best_no_bid) if self.kalshi_book.best_no_bid else None,
                "best_no_ask": float(self.kalshi_book.best_no_ask) if self.kalshi_book.best_no_ask else None,
                "best_yes_ask_size": float(self.kalshi_book.best_yes_ask_size) if self.kalshi_book.best_yes_ask_size else None,
                "timestamp_ns": self.kalshi_book.timestamp_ns,
                "updated_at": self.kalshi_book.updated_at.isoformat(),
            }

        opps_list = []
        for opp in self.latest_opportunities:
            opps_list.append({
                "opportunity_id": opp.opportunity_id,
                "direction": opp.direction,
                "yes_venue": opp.yes_venue.value,
                "no_venue": opp.no_venue.value,
                "yes_ask": float(opp.yes_ask),
                "no_ask": float(opp.no_ask),
                "raw_combined_cost": float(opp.raw_combined_cost),
                "theoretical_gross_edge": float(opp.theoretical_gross_edge),
                "poly_fee": float(opp.poly_fee),
                "kalshi_fee": float(opp.kalshi_fee),
                "total_fees": float(opp.total_fees),
                "expected_slippage": float(opp.expected_slippage),
                "latency_risk_buffer": float(opp.latency_risk_buffer),
                "execution_risk_buffer": float(opp.execution_risk_buffer),
                "unwind_risk_buffer": float(opp.unwind_risk_buffer),
                "min_profit_reserve": float(opp.min_profit_reserve),
                "conservative_net_edge": float(opp.conservative_net_edge),
                "executable_max_yes_price": float(opp.executable_max_yes_price),
                "executable_max_no_price": float(opp.executable_max_no_price),
                "is_executable": opp.is_executable,
                "rejection_reason": opp.rejection_reason,
            })

        trades_list = []
        for tr in self.recent_trades[-20:]:
            trades_list.append({
                "trade_id": tr.trade_id,
                "mode": tr.mode,
                "market_window": tr.market_window,
                "direction": tr.direction,
                "is_hedged": tr.is_hedged,
                "recovery_action": tr.recovery_action,
                "total_cost": float(tr.total_cost),
                "fees_paid": float(tr.fees_paid),
                "realized_pnl": float(tr.realized_pnl),
                "created_at": tr.created_at.strftime("%H:%M:%S"),
            })

        return {
            "system": {
                "is_running": self.is_running,
                "trading_mode": self.trading_mode,
                "kill_switch_active": self.kill_switch_active,
                "kill_switch_reason": self.kill_switch_reason,
                "polymarket_connected": self.polymarket_connected,
                "kalshi_connected": self.kalshi_connected,
                "server_time": datetime.now(timezone.utc).isoformat(),
            },
            "market_match": {
                "status": self.equivalence_result.status if self.equivalence_result else "PENDING",
                "is_equivalent": self.equivalence_result.is_equivalent if self.equivalence_result else False,
                "reason": self.equivalence_result.reason if self.equivalence_result else "No market comparison evaluated yet",
                "details": self.equivalence_result.details if self.equivalence_result else {},
            },
            "markets": {
                "polymarket": poly_mkt_dict,
                "kalshi": kalshi_mkt_dict,
            },
            "order_books": {
                "polymarket": poly_b,
                "kalshi": kalshi_b,
            },
            "opportunities": opps_list,
            "portfolio": self.portfolio_summary,
            "latency": self.latency_summary,
            "recent_trades": trades_list,
            "recent_rejections": self.recent_rejections[-20:],
        }


# Global dashboard state singleton
dashboard_state = DashboardState()
