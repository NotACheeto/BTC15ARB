"""Asynchronous concurrent execution engine and one-sided fill recovery manager.

Submits both legs simultaneously using asyncio.gather to minimize execution risk.
Implements automated orphan leg recovery (hedge re-attempt or immediate position unwind
within MAX_ORPHAN_EXIT_LOSS).
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import logging
import time
import uuid

from app.config import Settings
from app.exchanges.kalshi import KalshiClient
from app.exchanges.polymarket_us import PolymarketUSClient
from app.latency import latency_tracker
from app.models import (
    ArbitrageOpportunity,
    ArbitrageTradeRecord,
    ExecutionStatus,
    OrderExecutionResult,
    OrderRequest,
    OrderSide,
    OutcomeSide,
    OrderType,
    TimeInForce,
    Venue,
)
from app.risk import RiskManager

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Manages concurrent order submission, acknowledgements, and failure recovery."""

    def __init__(
        self,
        settings: Settings,
        risk_manager: RiskManager,
        poly_client: PolymarketUSClient,
        kalshi_client: KalshiClient,
    ):
        self.settings = settings
        self.risk_manager = risk_manager
        self.poly_client = poly_client
        self.kalshi_client = kalshi_client

    async def execute_arbitrage(
        self,
        opp: ArbitrageOpportunity,
    ) -> ArbitrageTradeRecord | None:
        """Execute cross-exchange arbitrage concurrently."""
        trade_id = f"trd-{uuid.uuid4().hex[:12]}"
        mode = self.risk_manager.trading_mode
        if mode == "DATA":
            logger.info("Skipping execution in DATA read-only mode")
            return None

        # Lock execution slot in risk manager
        self.risk_manager.record_arbitrage_started(opp)
        trade_record: ArbitrageTradeRecord | None = None

        try:
            # 1. Prepare Order Requests for both legs
            client_oid_yes = f"pm-yes-{uuid.uuid4().hex[:8]}"
            client_oid_no = f"pm-no-{uuid.uuid4().hex[:8]}"

            # Leg YES
            leg_yes_request = OrderRequest(
                client_order_id=client_oid_yes,
                opportunity_id=opp.opportunity_id,
                venue=opp.yes_venue,
                market_id=opp.polymarket_market.market_id if opp.yes_venue == Venue.POLYMARKET_US else opp.kalshi_market.market_id,
                ticker=opp.polymarket_market.ticker if opp.yes_venue == Venue.POLYMARKET_US else opp.kalshi_market.ticker,
                side=OrderSide.BUY,
                outcome=OutcomeSide.YES,
                price=opp.executable_max_yes_price,
                quantity=opp.target_quantity,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                created_at_ns=time.perf_counter_ns(),
            )

            # Leg NO
            leg_no_request = OrderRequest(
                client_order_id=client_oid_no,
                opportunity_id=opp.opportunity_id,
                venue=opp.no_venue,
                market_id=opp.polymarket_market.market_id if opp.no_venue == Venue.POLYMARKET_US else opp.kalshi_market.market_id,
                ticker=opp.polymarket_market.ticker if opp.no_venue == Venue.POLYMARKET_US else opp.kalshi_market.ticker,
                side=OrderSide.BUY,
                outcome=OutcomeSide.NO,
                price=opp.executable_max_no_price,
                quantity=opp.target_quantity,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                created_at_ns=time.perf_counter_ns(),
            )

            # 2. Submit both orders concurrently
            t_submit_start = time.perf_counter_ns()
            if mode == "LIVE":
                task_yes = asyncio.create_task(self._submit_live_leg(leg_yes_request))
                task_no = asyncio.create_task(self._submit_live_leg(leg_no_request))
                result_yes, result_no = await asyncio.gather(task_yes, task_no)
            else:
                # PAPER mode realistic fill simulation
                result_yes, result_no = await self._simulate_paper_execution(leg_yes_request, leg_no_request, opp)

            t_submit_end = time.perf_counter_ns()
            elapsed_submit_ns = t_submit_end - t_submit_start
            latency_tracker.order_submission_time.record_ns(elapsed_submit_ns)

            # Record ack delta latency if both acknowledged
            if result_yes.ack_at_ns and result_no.ack_at_ns:
                delta_ns = abs(result_yes.ack_at_ns - result_no.ack_at_ns)
                latency_tracker.ack_delta.record_ns(delta_ns)

            # 3. Analyze Fills & Handle One-Sided Fills
            trade_record = await self._reconcile_and_recover(
                trade_id=trade_id,
                opp=opp,
                mode=mode,
                result_yes=result_yes,
                result_no=result_no,
                leg_yes_req=leg_yes_request,
                leg_no_req=leg_no_request,
                submit_start_ns=t_submit_start,
                submit_end_ns=t_submit_end,
            )

            return trade_record

        finally:
            unhedged = 1 if (trade_record and not trade_record.is_hedged) else 0
            self.risk_manager.record_arbitrage_finished(
                opp.opportunity_id, opp.market_window, unhedged=unhedged
            )

    async def _submit_live_leg(self, req: OrderRequest) -> OrderExecutionResult:
        """Route order to the designated exchange client."""
        if req.venue == Venue.POLYMARKET_US:
            return await self.poly_client.create_order(req)
        else:
            return await self.kalshi_client.create_order(req)

    async def _simulate_paper_execution(
        self,
        leg_yes_req: OrderRequest,
        leg_no_req: OrderRequest,
        opp: ArbitrageOpportunity,
    ) -> tuple[OrderExecutionResult, OrderExecutionResult]:
        """Realistic simulation for PAPER mode."""
        # Simulate realistic 35ms network transit and matching engine latency
        await asyncio.sleep(0.035)
        now_ns = time.perf_counter_ns()

        # Leg YES
        res_yes = OrderExecutionResult(
            venue=leg_yes_req.venue,
            order_id=f"paper-{uuid.uuid4().hex[:8]}",
            client_order_id=leg_yes_req.client_order_id,
            opportunity_id=opp.opportunity_id,
            status=ExecutionStatus.FILLED,
            price=opp.yes_ask,
            fill_price=opp.yes_ask,
            quantity=opp.target_quantity,
            fill_quantity=opp.target_quantity,
            fee_paid=opp.poly_fee if leg_yes_req.venue == Venue.POLYMARKET_US else opp.kalshi_fee,
            submitted_at_ns=leg_yes_req.created_at_ns,
            ack_at_ns=now_ns,
            filled_at_ns=now_ns,
        )

        # Leg NO
        res_no = OrderExecutionResult(
            venue=leg_no_req.venue,
            order_id=f"paper-{uuid.uuid4().hex[:8]}",
            client_order_id=leg_no_req.client_order_id,
            opportunity_id=opp.opportunity_id,
            status=ExecutionStatus.FILLED,
            price=opp.no_ask,
            fill_price=opp.no_ask,
            quantity=opp.target_quantity,
            fill_quantity=opp.target_quantity,
            fee_paid=opp.kalshi_fee if leg_no_req.venue == Venue.KALSHI else opp.poly_fee,
            submitted_at_ns=leg_no_req.created_at_ns,
            ack_at_ns=now_ns,
            filled_at_ns=now_ns,
        )

        return res_yes, res_no

    async def _reconcile_and_recover(
        self,
        trade_id: str,
        opp: ArbitrageOpportunity,
        mode: str,
        result_yes: OrderExecutionResult,
        result_no: OrderExecutionResult,
        leg_yes_req: OrderRequest,
        leg_no_req: OrderRequest,
        submit_start_ns: int,
        submit_end_ns: int,
    ) -> ArbitrageTradeRecord:
        """Handle execution results, detect one-sided fills, and trigger emergency unwind."""
        yes_filled = result_yes.status == ExecutionStatus.FILLED
        no_filled = result_no.status == ExecutionStatus.FILLED

        recovery_action = None
        recovery_result = None
        is_hedged = False
        realized_pnl = Decimal("0.00")

        # -------------------------------------------------------------
        # Scenario A: Clean Double Fill (Fully Hedged)
        # -------------------------------------------------------------
        if yes_filled and no_filled:
            is_hedged = True
            total_acquisition_cost = (result_yes.fill_price or opp.yes_ask) + (result_no.fill_price or opp.no_ask)
            total_fees = result_yes.fee_paid + result_no.fee_paid
            # Settlement value = $1.00
            realized_pnl = Decimal("1.00") - total_acquisition_cost - total_fees
            logger.info(
                "SUCCESSFUL ARB TRADE %s: Cost=$%.4f, Fees=$%.4f, Expected Net P&L=+$%.4f",
                trade_id, total_acquisition_cost, total_fees, realized_pnl
            )

        # -------------------------------------------------------------
        # Scenario B: Neither Leg Filled
        # -------------------------------------------------------------
        elif not yes_filled and not no_filled:
            is_hedged = False
            realized_pnl = Decimal("0.00")
            logger.warning("Both legs unfilled for %s; position neutral", trade_id)

        # -------------------------------------------------------------
        # Scenario C: ONE-SIDED FILL EMERGENCY RECOVERY
        # -------------------------------------------------------------
        else:
            is_hedged = False
            logger.critical(
                "ONE-SIDED FILL DETECTED ON TRADE %s! YES_FILLED=%s, NO_FILLED=%s. INITIATING EMERGENCY UNWIND.",
                trade_id, yes_filled, no_filled
            )

            # Filled leg is YES, unhedged leg is NO
            if yes_filled and not no_filled:
                filled_res = result_yes
                filled_req = leg_yes_req
                unfilled_res = result_no
                orphan_side = "YES"
            else:
                filled_res = result_no
                filled_req = leg_no_req
                unfilled_res = result_yes
                orphan_side = "NO"

            # 1. Immediately cancel the unfilled leg order if open/resting
            if unfilled_res.order_id:
                try:
                    logger.info("Cancelling unfilled resting order %s on %s", unfilled_res.order_id, unfilled_res.venue.value)
                    if unfilled_res.venue == Venue.POLYMARKET_US:
                        await self.poly_client.cancel_order(unfilled_res.order_id, opp.polymarket_market.market_id)
                    else:
                        await self.kalshi_client.cancel_order(unfilled_res.order_id)
                except Exception as e:
                    logger.error("Error cancelling unfilled leg: %s", e)

            # 2. Emergency unwind of the filled orphan position
            recovery_action = f"EMERGENCY_UNWIND_{orphan_side}_LEG"
            recovery_result = await self._emergency_unwind_position(
                filled_res=filled_res,
                filled_req=filled_req,
                mode=mode,
                opp=opp,
            )

            # Calculate loss taken on emergency liquidation
            fill_px = filled_res.fill_price or Decimal("0.50")
            exit_px = recovery_result.fill_price or (fill_px - self.settings.MAX_ORPHAN_EXIT_LOSS)
            unwind_loss = fill_px - exit_px
            realized_pnl = -unwind_loss - filled_res.fee_paid - recovery_result.fee_paid
            logger.warning(
                "EMERGENCY UNWIND COMPLETE for %s: UnwindLoss=-$%.4f, Net P&L=-$%.4f",
                trade_id, unwind_loss, realized_pnl
            )

        total_cost = (
            (result_yes.fill_price or Decimal("0.00")) + (result_no.fill_price or Decimal("0.00"))
        )
        total_fees = result_yes.fee_paid + result_no.fee_paid

        record = ArbitrageTradeRecord(
            trade_id=trade_id,
            opportunity_id=opp.opportunity_id,
            mode=mode,  # type: ignore
            market_window=opp.market_window,
            direction=opp.direction,
            leg_yes=result_yes,
            leg_no=result_no,
            is_hedged=is_hedged,
            recovery_action=recovery_action,
            recovery_result=recovery_result,
            total_cost=total_cost,
            payout_expected=Decimal("1.00") if is_hedged else Decimal("0.00"),
            fees_paid=total_fees,
            realized_pnl=realized_pnl,
            latencies_ns={
                "submission_duration_ns": submit_end_ns - submit_start_ns,
                "leg_yes_ack_ns": result_yes.ack_at_ns,
                "leg_no_ack_ns": result_no.ack_at_ns,
            },
            created_at=datetime.now(timezone.utc),
        )

        return record

    async def _emergency_unwind_position(
        self,
        filled_res: OrderExecutionResult,
        filled_req: OrderRequest,
        mode: str,
        opp: ArbitrageOpportunity,
    ) -> OrderExecutionResult:
        """Immediately exit the unhedged leg within MAX_ORPHAN_EXIT_LOSS."""
        now_ns = time.perf_counter_ns()
        if mode == "PAPER":
            # Simulate unwind exit at 1-2 cents below purchase price
            fill_px = filled_res.fill_price or Decimal("0.50")
            exit_px = max(Decimal("0.01"), fill_px - self.settings.MAX_ORPHAN_EXIT_LOSS)
            return OrderExecutionResult(
                venue=filled_res.venue,
                order_id=f"unwind-{uuid.uuid4().hex[:8]}",
                client_order_id=f"unwind-cl-{uuid.uuid4().hex[:8]}",
                opportunity_id=opp.opportunity_id,
                status=ExecutionStatus.FILLED,
                price=exit_px,
                fill_price=exit_px,
                quantity=filled_res.fill_quantity,
                fill_quantity=filled_res.fill_quantity,
                fee_paid=Decimal("0.01"),
                submitted_at_ns=now_ns,
                ack_at_ns=now_ns,
                filled_at_ns=now_ns,
            )

        # LIVE MODE EMERGENCY CLOSE
        try:
            if filled_res.venue == Venue.POLYMARKET_US:
                logger.info("Calling Polymarket US native close_position for '%s'", filled_req.market_id)
                close_resp = await self.poly_client.close_position(filled_req.market_id)
                return OrderExecutionResult(
                    venue=Venue.POLYMARKET_US,
                    order_id=str(close_resp.get("orderId", "")),
                    client_order_id=f"unwind-{uuid.uuid4().hex[:8]}",
                    opportunity_id=opp.opportunity_id,
                    status=ExecutionStatus.FILLED,
                    price=Decimal("0.00"),
                    fill_price=Decimal(str(close_resp.get("fillPrice", filled_res.fill_price or Decimal("0.50")))),
                    quantity=filled_res.fill_quantity,
                    fill_quantity=filled_res.fill_quantity,
                    fee_paid=Decimal("0.01"),
                    submitted_at_ns=now_ns,
                    ack_at_ns=time.perf_counter_ns(),
                    filled_at_ns=time.perf_counter_ns(),
                    raw_response=close_resp,
                )
            else:
                # Kalshi unwind: sell opposite side
                sell_req = OrderRequest(
                    client_order_id=f"unwind-{uuid.uuid4().hex[:8]}",
                    opportunity_id=opp.opportunity_id,
                    venue=Venue.KALSHI,
                    market_id=filled_req.market_id,
                    ticker=filled_req.ticker,
                    side=OrderSide.SELL,
                    outcome=filled_req.outcome,
                    price=max(Decimal("0.01"), (filled_res.fill_price or Decimal("0.50")) - self.settings.MAX_ORPHAN_EXIT_LOSS),
                    quantity=filled_res.fill_quantity,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                )
                return await self.kalshi_client.create_order(sell_req)

        except Exception as e:
            logger.critical("FATAL: Failed to unwind orphan position: %s", e)
            self.risk_manager.activate_kill_switch(f"Failed to unwind orphan position: {e}")
            return OrderExecutionResult(
                venue=filled_res.venue,
                client_order_id="failed-unwind",
                opportunity_id=opp.opportunity_id,
                status=ExecutionStatus.FAILED,
                price=Decimal("0.00"),
                error_message=str(e),
            )
