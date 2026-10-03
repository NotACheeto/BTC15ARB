"""Arbitrage opportunity detection and conservative executable edge engine.

Evaluates both trading directions (Buy Poly YES + Buy Kalshi NO, and Buy Kalshi YES + Buy Poly NO).
Deducts all fees, slippage, dynamic latency buffer, unwind risk buffer, and profit reserve
using exact Decimal arithmetic.
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
import time
import uuid

from app.config import Settings
from app.fees import calculate_total_arb_fees
from app.latency import latency_tracker
from app.models import (
    ArbitrageOpportunity,
    NormalizedMarket,
    OrderBookState,
    Venue,
)

logger = logging.getLogger(__name__)

ONE = Decimal("1.00")
ZERO = Decimal("0.00")


class ArbitrageCalculator:
    """Calculates gross and conservative net executable edges across venues."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate_opportunity(
        self,
        poly_market: NormalizedMarket,
        kalshi_market: NormalizedMarket,
        poly_book: OrderBookState,
        kalshi_book: OrderBookState,
    ) -> list[ArbitrageOpportunity]:
        """Evaluate both arb directions and return detailed ArbitrageOpportunity objects."""
        now_ns = time.perf_counter_ns()
        opportunities: list[ArbitrageOpportunity] = []

        # Staleness checks
        max_age_ns = self.settings.MAX_MARKET_DATA_AGE_MS * 1_000_000
        poly_age_ns = now_ns - poly_book.timestamp_ns
        kalshi_age_ns = now_ns - kalshi_book.timestamp_ns

        poly_stale = poly_age_ns > max_age_ns
        kalshi_stale = kalshi_age_ns > max_age_ns

        # Dynamic latency risk haircut
        latency_buffer = latency_tracker.calculate_latency_risk_buffer(
            mode=self.settings.LATENCY_BUFFER_MODE
        )

        market_window = (
            f"{poly_market.interval_start.isoformat()}-{poly_market.interval_end.isoformat()}"
        )

        # -------------------------------------------------------------
        # Direction 1: Buy Polymarket YES + Buy Kalshi NO
        # -------------------------------------------------------------
        opp1 = self._calculate_direction(
            direction="BUY_POLY_YES_BUY_KALSHI_NO",
            yes_venue=Venue.POLYMARKET_US,
            no_venue=Venue.KALSHI,
            yes_ask=poly_book.best_yes_ask,
            no_ask=kalshi_book.best_no_ask,
            yes_depth=poly_book.best_yes_ask_size or Decimal("1"),
            no_depth=kalshi_book.best_no_ask_size or Decimal("1"),
            poly_market=poly_market,
            kalshi_market=kalshi_market,
            market_window=market_window,
            poly_stale=poly_stale,
            kalshi_stale=kalshi_stale,
            latency_buffer=latency_buffer,
            now_ns=now_ns,
        )
        opportunities.append(opp1)

        # -------------------------------------------------------------
        # Direction 2: Buy Kalshi YES + Buy Polymarket NO
        # -------------------------------------------------------------
        opp2 = self._calculate_direction(
            direction="BUY_KALSHI_YES_BUY_POLY_NO",
            yes_venue=Venue.KALSHI,
            no_venue=Venue.POLYMARKET_US,
            yes_ask=kalshi_book.best_yes_ask,
            no_ask=poly_book.best_no_ask,
            yes_depth=kalshi_book.best_yes_ask_size or Decimal("1"),
            no_depth=poly_book.best_no_ask_size or Decimal("1"),
            poly_market=poly_market,
            kalshi_market=kalshi_market,
            market_window=market_window,
            poly_stale=poly_stale,
            kalshi_stale=kalshi_stale,
            latency_buffer=latency_buffer,
            now_ns=now_ns,
        )
        opportunities.append(opp2)

        return opportunities

    def _calculate_direction(
        self,
        direction: str,
        yes_venue: Venue,
        no_venue: Venue,
        yes_ask: Decimal | None,
        no_ask: Decimal | None,
        yes_depth: Decimal,
        no_depth: Decimal,
        poly_market: NormalizedMarket,
        kalshi_market: NormalizedMarket,
        market_window: str,
        poly_stale: bool,
        kalshi_stale: bool,
        latency_buffer: Decimal,
        now_ns: int,
    ) -> ArbitrageOpportunity:
        opp_id = f"opp-{uuid.uuid4().hex[:10]}"
        target_qty = self.settings.MAX_CONTRACTS_PER_LEG  # Default: 1

        # Missing quote check
        if yes_ask is None or no_ask is None:
            return ArbitrageOpportunity(
                opportunity_id=opp_id,
                market_window=market_window,
                polymarket_market=poly_market,
                kalshi_market=kalshi_market,
                direction=direction,  # type: ignore
                yes_venue=yes_venue,
                no_venue=no_venue,
                yes_ask=yes_ask or ZERO,
                no_ask=no_ask or ZERO,
                raw_combined_cost=ZERO,
                theoretical_gross_edge=ZERO,
                poly_fee=ZERO,
                kalshi_fee=ZERO,
                total_fees=ZERO,
                expected_slippage=ZERO,
                latency_risk_buffer=ZERO,
                execution_risk_buffer=ZERO,
                unwind_risk_buffer=ZERO,
                min_profit_reserve=ZERO,
                conservative_net_edge=ZERO,
                executable_max_yes_price=ZERO,
                executable_max_no_price=ZERO,
                is_executable=False,
                rejection_reason="Missing quote on one or both order books",
                detected_at=datetime.now(timezone.utc),
                detected_monotonic_ns=now_ns,
            )

        # 1. Raw combined acquisition cost
        raw_combined_cost = yes_ask + no_ask
        theoretical_gross_edge = ONE - raw_combined_cost

        # 2. Documented fees
        poly_fee, kalshi_fee, total_fees = calculate_total_arb_fees(
            yes_price=yes_ask,
            no_price=no_ask,
            yes_venue=yes_venue,
            no_venue=no_venue,
            quantity=target_qty,
            poly_coeff=poly_market.fee_coefficient,
            kalshi_coeff=self.settings.KALSHI_TAKER_FEE_COEFF,
        )

        # 3. Execution risk buffers & haircut
        expected_slippage = self.settings.DEFAULT_SLIPPAGE_BUFFER
        unwind_risk = self.settings.UNWIND_RISK_BUFFER
        execution_risk = Decimal("0.005")  # 0.5 cents baseline execution uncertainty
        profit_reserve = self.settings.MIN_PROFIT_RESERVE

        # Total deductions
        total_deductions = (
            total_fees
            + expected_slippage
            + latency_buffer
            + execution_risk
            + unwind_risk
            + profit_reserve
        )

        # 4. Conservative net executable edge
        conservative_net_edge = theoretical_gross_edge - total_deductions

        # 5. Bound maximum acceptable execution limit prices
        # Guarantee that even in the worst-case scenario where both limit orders fill at their maximum limits,
        # total acquisition cost + total fees never exceeds $1.00 - MIN_PROFIT_RESERVE.
        max_total_cost = ONE - total_fees - profit_reserve
        margin_above_cost = max(ZERO, max_total_cost - raw_combined_cost)
        slippage_per_leg = min(expected_slippage / Decimal("2"), margin_above_cost / Decimal("2"))

        max_yes_px = min(ONE - Decimal("0.01"), (yes_ask + slippage_per_leg).quantize(Decimal("0.01")))
        max_no_px = min(ONE - Decimal("0.01"), (no_ask + slippage_per_leg).quantize(Decimal("0.01")))

        if max_yes_px + max_no_px > max_total_cost:
            excess = (max_yes_px + max_no_px) - max_total_cost
            max_yes_px -= (excess / Decimal("2")).quantize(Decimal("0.01"))
            max_no_px = max_total_cost - max_yes_px

        # 6. Strict executable validation & rejection reasoning
        is_executable = True
        rejection_reason = None

        if poly_stale:
            is_executable = False
            rejection_reason = "Polymarket market data is stale"
        elif kalshi_stale:
            is_executable = False
            rejection_reason = "Kalshi market data is stale"
        elif min(yes_depth, no_depth) < target_qty:
            is_executable = False
            rejection_reason = f"Insufficient book depth: available={min(yes_depth, no_depth)}, target={target_qty}"
        elif theoretical_gross_edge <= ZERO:
            is_executable = False
            rejection_reason = f"Negative theoretical gross edge: {theoretical_gross_edge:.4f}"
        elif conservative_net_edge < self.settings.MIN_NET_EDGE:
            is_executable = False
            rejection_reason = (
                f"Net edge {conservative_net_edge:.4f} below MIN_NET_EDGE threshold {self.settings.MIN_NET_EDGE:.4f} "
                f"(Fees={total_fees:.4f}, LatencyHaircut={latency_buffer:.4f})"
            )

        return ArbitrageOpportunity(
            opportunity_id=opp_id,
            market_window=market_window,
            polymarket_market=poly_market,
            kalshi_market=kalshi_market,
            direction=direction,  # type: ignore
            yes_venue=yes_venue,
            no_venue=no_venue,
            yes_ask=yes_ask,
            no_ask=no_ask,
            available_quantity=int(min(yes_depth, no_depth)),
            target_quantity=target_qty,
            payout=ONE,
            raw_combined_cost=raw_combined_cost,
            theoretical_gross_edge=theoretical_gross_edge,
            poly_fee=poly_fee,
            kalshi_fee=kalshi_fee,
            total_fees=total_fees,
            expected_slippage=expected_slippage,
            latency_risk_buffer=latency_buffer,
            execution_risk_buffer=execution_risk,
            unwind_risk_buffer=unwind_risk,
            min_profit_reserve=profit_reserve,
            conservative_net_edge=conservative_net_edge,
            executable_max_yes_price=max_yes_px,
            executable_max_no_price=max_no_px,
            is_executable=is_executable,
            rejection_reason=rejection_reason,
            detected_at=datetime.now(timezone.utc),
            detected_monotonic_ns=now_ns,
        )
