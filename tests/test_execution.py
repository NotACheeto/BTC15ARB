"""Unit tests for concurrent leg execution and one-sided fill emergency recovery."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from app.config import get_settings
from app.exchanges.kalshi import KalshiClient
from app.exchanges.polymarket_us import PolymarketUSClient
from app.execution import ExecutionEngine
from app.models import ArbitrageOpportunity, ExecutionStatus, NormalizedMarket, Venue
from app.risk import RiskManager


@pytest.fixture
def execution_fixture():
    settings = get_settings()
    settings.PAPER_TRADING = True
    settings.MAX_ORPHAN_EXIT_LOSS = Decimal("0.02")

    risk = RiskManager(settings)
    risk.trading_mode = "PAPER"

    poly_client = PolymarketUSClient(settings)
    kalshi_client = KalshiClient(settings)
    engine = ExecutionEngine(settings, risk, poly_client, kalshi_client)

    now = datetime.now(timezone.utc)
    poly = NormalizedMarket(
        venue=Venue.POLYMARKET_US,
        market_id="p-1",
        ticker="poly-test",
        title="BTC",
        description="",
        underlying="BTC",
        market_type="15M",
        interval_start=now,
        interval_end=now + timedelta(minutes=15),
        duration_seconds=900,
        outcome_yes_definition="",
        outcome_no_definition="",
        reference_source="BRTI",
        settlement_method="60S_BRTI_AVERAGE",
        payout=Decimal("1.00"),
        status="active",
    )
    kalshi = NormalizedMarket(
        venue=Venue.KALSHI,
        market_id="k-1",
        ticker="kalshi-test",
        title="BTC",
        description="",
        underlying="BTC",
        market_type="15M",
        interval_start=now,
        interval_end=now + timedelta(minutes=15),
        duration_seconds=900,
        outcome_yes_definition="",
        outcome_no_definition="",
        reference_source="BRTI",
        settlement_method="60S_BRTI_AVERAGE",
        payout=Decimal("1.00"),
        status="active",
    )

    opp = ArbitrageOpportunity(
        opportunity_id="opp-exec-1",
        market_window="2026-10-03T07:00Z-07:15Z",
        polymarket_market=poly,
        kalshi_market=kalshi,
        direction="BUY_POLY_YES_BUY_KALSHI_NO",
        yes_venue=Venue.POLYMARKET_US,
        no_venue=Venue.KALSHI,
        yes_ask=Decimal("0.55"),
        no_ask=Decimal("0.35"),
        target_quantity=1,
        raw_combined_cost=Decimal("0.90"),
        theoretical_gross_edge=Decimal("0.10"),
        poly_fee=Decimal("0.02"),
        kalshi_fee=Decimal("0.02"),
        total_fees=Decimal("0.04"),
        expected_slippage=Decimal("0.01"),
        latency_risk_buffer=Decimal("0.005"),
        execution_risk_buffer=Decimal("0.005"),
        unwind_risk_buffer=Decimal("0.01"),
        min_profit_reserve=Decimal("0.01"),
        conservative_net_edge=Decimal("0.01"),
        executable_max_yes_price=Decimal("0.56"),
        executable_max_no_price=Decimal("0.36"),
        is_executable=True,
    )

    return engine, opp


@pytest.mark.asyncio
async def test_paper_execution_both_fill_hedged(execution_fixture):
    engine, opp = execution_fixture
    trade = await engine.execute_arbitrage(opp)

    assert trade is not None
    assert trade.is_hedged is True
    assert trade.leg_yes.status == ExecutionStatus.FILLED
    assert trade.leg_no.status == ExecutionStatus.FILLED
    assert trade.realized_pnl > Decimal("0.00")  # Positive EV realized
    assert trade.recovery_action is None


@pytest.mark.asyncio
async def test_one_sided_fill_emergency_recovery(execution_fixture, monkeypatch):
    engine, opp = execution_fixture

    orig_sim = engine._simulate_paper_execution

    # Simulate one-sided fill: leg YES fills, leg NO rejects
    async def mock_sim_one_sided(leg_yes_req, leg_no_req, opp):
        res_yes, res_no = await orig_sim(leg_yes_req, leg_no_req, opp)
        # Force leg NO to fail/reject
        res_no.status = ExecutionStatus.REJECTED
        res_no.fill_quantity = 0
        res_no.fill_price = None
        return res_yes, res_no

    monkeypatch.setattr(engine, "_simulate_paper_execution", mock_sim_one_sided)

    trade = await engine.execute_arbitrage(opp)

    assert trade is not None
    assert trade.is_hedged is False
    assert trade.recovery_action is not None
    assert "EMERGENCY_UNWIND" in trade.recovery_action
    assert trade.recovery_result is not None
    assert trade.recovery_result.status == ExecutionStatus.FILLED
    # Realized loss is bounded by MAX_ORPHAN_EXIT_LOSS (0.02) + fees
    assert trade.realized_pnl <= Decimal("0.00")
    assert abs(trade.realized_pnl) <= Decimal("0.05")
