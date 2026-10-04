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
            unhedged_remaining = 0
            orphan_qty = 0
            if trade_record and not trade_record.is_hedged:
                orphan_qty = abs(trade_record.leg_yes.fill_quantity - trade_record.leg_no.fill_quantity)
                liquidated_qty = (
                    trade_record.recovery_result.fill_quantity
                    if (trade_record.recovery_result and trade_record.recovery_result.status == ExecutionStatus.FILLED)
                    else 0
                )
                unhedged_remaining = max(0, orphan_qty - liquidated_qty)

            self.risk_manager.record_arbitrage_finished(
                opp.opportunity_id,
                opp.market_window,
                unhedged=unhedged_remaining,
                orphan_encountered=orphan_qty,
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
        """Handle execution results, accurately reconcile partial fills, and trigger emergency IOC unwind."""
        yes_qty = result_yes.fill_quantity if result_yes.status in (ExecutionStatus.FILLED, ExecutionStatus.PARTIALLY_FILLED) else 0
        no_qty = result_no.fill_quantity if result_no.status in (ExecutionStatus.FILLED, ExecutionStatus.PARTIALLY_FILLED) else 0

        hedged_qty = min(yes_qty, no_qty)
        orphan_qty = abs(yes_qty - no_qty)

        recovery_action = None
        recovery_result = None
        is_hedged = False
        realized_pnl = Decimal("0.00")

        # -------------------------------------------------------------
        # Scenario A: Clean Symmetrical Fill (Fully Hedged)
        # -------------------------------------------------------------
        if yes_qty > 0 and no_qty > 0 and yes_qty == no_qty:
            is_hedged = True
            total_acquisition_cost = (
                (result_yes.fill_price or opp.yes_ask) * Decimal(hedged_qty)
                + (result_no.fill_price or opp.no_ask) * Decimal(hedged_qty)
            )
            total_fees = result_yes.fee_paid + result_no.fee_paid
            # Settlement value = $1.00 per hedged contract
            realized_pnl = (Decimal("1.00") * Decimal(hedged_qty)) - total_acquisition_cost - total_fees
            logger.info(
                "SUCCESSFUL ARB TRADE %s: HedgedQty=%d, Cost=$%.4f, Fees=$%.4f, Expected Net P&L=+$%.4f",
                trade_id, hedged_qty, total_acquisition_cost, total_fees, realized_pnl
            )

        # -------------------------------------------------------------
        # Scenario B: Neither Leg Filled
        # -------------------------------------------------------------
        elif yes_qty == 0 and no_qty == 0:
            is_hedged = False
            realized_pnl = Decimal("0.00")
            logger.warning("Both legs unfilled for %s; position neutral", trade_id)

            # Cancel any resting orders that might linger
            for res, req in ((result_yes, leg_yes_req), (result_no, leg_no_req)):
                if res.order_id and res.status == ExecutionStatus.ACKNOWLEDGED:
                    try:
                        if res.venue == Venue.POLYMARKET_US:
                            await self.poly_client.cancel_order(res.order_id, req.market_id)
                        else:
                            await self.kalshi_client.cancel_order(res.order_id)
                    except Exception as e:
                        logger.warning("Error cleaning up unfilled order: %s", e)

        # -------------------------------------------------------------
        # Scenario C: ONE-SIDED OR ASYMMETRICAL PARTIAL FILL (ORPHAN UNWIND)
        # -------------------------------------------------------------
        else:
            is_hedged = False
            logger.critical(
                "ASYMMETRICAL/ONE-SIDED FILL ON TRADE %s! YES_QTY=%d, NO_QTY=%d, ORPHAN_QTY=%d. INITIATING TARGETED IOC UNWIND.",
                trade_id, yes_qty, no_qty, orphan_qty
            )

            # Identify orphan side
            if yes_qty > no_qty:
                orphan_res = result_yes
                orphan_req = leg_yes_req
                underfilled_res = result_no
                underfilled_req = leg_no_req
                orphan_side = "YES"
            else:
                orphan_res = result_no
                orphan_req = leg_no_req
                underfilled_res = result_yes
                underfilled_req = leg_yes_req
                orphan_side = "NO"

            # 1. Immediately cancel any resting remainder on underfilled leg
            if underfilled_res.order_id and underfilled_res.status not in (ExecutionStatus.FILLED, ExecutionStatus.REJECTED):
                try:
                    logger.info("Cancelling resting underfilled order %s on %s", underfilled_res.order_id, underfilled_res.venue.value)
                    if underfilled_res.venue == Venue.POLYMARKET_US:
                        await self.poly_client.cancel_order(underfilled_res.order_id, underfilled_req.market_id)
                    else:
                        await self.kalshi_client.cancel_order(underfilled_res.order_id)
                except Exception as e:
                    logger.error("Error cancelling underfilled leg remainder: %s", e)

            # 2. Targeted IOC opposite order emergency unwind of exactly orphan_qty
            recovery_action = f"EMERGENCY_UNWIND_IOC_{orphan_side}_LEG_{orphan_qty}X"
            recovery_result = await self._emergency_unwind_position(
                filled_res=orphan_res,
                filled_req=orphan_req,
                unwind_quantity=orphan_qty,
                mode=mode,
                opp=opp,
            )

            # 3. Accurate realized PnL reconciliation:
            # Hedged portion (if any partial symmetry existed)
            hedged_pnl = Decimal("0.00")
            if hedged_qty > 0:
                h_cost = (
                    (result_yes.fill_price or opp.yes_ask) * Decimal(hedged_qty)
                    + (result_no.fill_price or opp.no_ask) * Decimal(hedged_qty)
                )
                hedged_pnl = (Decimal("1.00") * Decimal(hedged_qty)) - h_cost

            # Orphan liquidation loss
            fill_px = orphan_res.fill_price or Decimal("0.50")
            exit_px = recovery_result.fill_price or (fill_px - self.settings.MAX_ORPHAN_EXIT_LOSS)
            unwind_loss = (fill_px - exit_px) * Decimal(orphan_qty)
            yes_fee = result_yes.fee_paid if result_yes.fill_quantity > 0 else Decimal("0.00")
            no_fee = result_no.fee_paid if result_no.fill_quantity > 0 else Decimal("0.00")
            recovery_fee = recovery_result.fee_paid if recovery_result else Decimal("0.00")
            total_fees = yes_fee + no_fee + recovery_fee
            realized_pnl = hedged_pnl - unwind_loss - total_fees

            logger.warning(
                "TARGETED UNWIND COMPLETE for %s: OrphanQty=%d, UnwindLoss=-$%.4f, Net P&L=-$%.4f",
                trade_id, orphan_qty, unwind_loss, realized_pnl
            )

        total_cost = (
            (result_yes.fill_price or Decimal("0.00")) * Decimal(result_yes.fill_quantity)
            + (result_no.fill_price or Decimal("0.00")) * Decimal(result_no.fill_quantity)
        )
        yes_fee = result_yes.fee_paid if result_yes.fill_quantity > 0 else Decimal("0.00")
        no_fee = result_no.fee_paid if result_no.fill_quantity > 0 else Decimal("0.00")
        recovery_fee = recovery_result.fee_paid if recovery_result else Decimal("0.00")
        total_fees = yes_fee + no_fee + recovery_fee

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
            payout_expected=(Decimal("1.00") * Decimal(hedged_qty)) if is_hedged else Decimal("0.00"),
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
        unwind_quantity: int,
        mode: str,
        opp: ArbitrageOpportunity,
    ) -> OrderExecutionResult:
        """Immediately exit the unhedged leg of exactly unwind_quantity within MAX_ORPHAN_EXIT_LOSS."""
        now_ns = time.perf_counter_ns()
        qty_to_unwind = unwind_quantity if unwind_quantity > 0 else filled_res.fill_quantity
        fill_px = filled_res.fill_price or Decimal("0.50")
        exit_px = max(Decimal("0.01"), fill_px - self.settings.MAX_ORPHAN_EXIT_LOSS)

        if mode == "PAPER":
            # Simulate unwind exit at 1-2 cents below purchase price
            return OrderExecutionResult(
                venue=filled_res.venue,
                order_id=f"unwind-{uuid.uuid4().hex[:8]}",
                client_order_id=f"unwind-cl-{uuid.uuid4().hex[:8]}",
                opportunity_id=opp.opportunity_id,
                status=ExecutionStatus.FILLED,
                price=exit_px,
                fill_price=exit_px,
                quantity=qty_to_unwind,
                fill_quantity=qty_to_unwind,
                fee_paid=Decimal("0.01"),
                submitted_at_ns=now_ns,
                ack_at_ns=now_ns,
                filled_at_ns=now_ns,
            )

        # LIVE MODE EMERGENCY CLOSE
        try:
            if filled_res.venue == Venue.POLYMARKET_US:
                logger.info(
                    "Executing targeted Polymarket US IOC sell unwind for %d contracts on '%s'",
                    qty_to_unwind, filled_req.market_id
                )
                sell_req = OrderRequest(
                    client_order_id=f"unwind-{uuid.uuid4().hex[:8]}",
                    opportunity_id=opp.opportunity_id,
                    venue=Venue.POLYMARKET_US,
                    market_id=filled_req.market_id,
                    ticker=filled_req.ticker,
                    side=OrderSide.SELL,
                    outcome=filled_req.outcome,
                    price=exit_px,
                    quantity=qty_to_unwind,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                )
                try:
                    res = await self.poly_client.create_order(sell_req)
                    if res.status == ExecutionStatus.FILLED and res.fill_quantity > 0:
                        return res
                except Exception as poly_err:
                    logger.warning("Targeted Polymarket IOC SELL failed (%s), attempting native close_position fallback", poly_err)

                # Fallback to native close_position if entire position was unhedged
                if qty_to_unwind == filled_res.fill_quantity:
                    logger.info("Calling Polymarket US native close_position for '%s'", filled_req.market_id)
                    close_resp = await self.poly_client.close_position(filled_req.market_id)
                    return OrderExecutionResult(
                        venue=Venue.POLYMARKET_US,
                        order_id=str(close_resp.get("orderId", "")),
                        client_order_id=f"unwind-{uuid.uuid4().hex[:8]}",
                        opportunity_id=opp.opportunity_id,
                        status=ExecutionStatus.FILLED,
                        price=Decimal("0.00"),
                        fill_price=Decimal(str(close_resp.get("fillPrice", fill_px))),
                        quantity=qty_to_unwind,
                        fill_quantity=qty_to_unwind,
                        fee_paid=Decimal("0.01"),
                        submitted_at_ns=now_ns,
                        ack_at_ns=time.perf_counter_ns(),
                        filled_at_ns=time.perf_counter_ns(),
                        raw_response=close_resp,
                    )
                raise RuntimeError(f"Polymarket US IOC unwind failed for {qty_to_unwind} contracts")
            else:
                # Kalshi unwind: targeted IOC SELL order
                logger.info("Executing Kalshi targeted IOC sell unwind for %d contracts on '%s'", qty_to_unwind, filled_req.ticker)
                sell_req = OrderRequest(
                    client_order_id=f"unwind-{uuid.uuid4().hex[:8]}",
                    opportunity_id=opp.opportunity_id,
                    venue=Venue.KALSHI,
                    market_id=filled_req.market_id,
                    ticker=filled_req.ticker,
                    side=OrderSide.SELL,
                    outcome=filled_req.outcome,
                    price=exit_px,
                    quantity=qty_to_unwind,
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
