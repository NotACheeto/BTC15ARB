"""Unit tests for the risk manager, kill switch, and position limits."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from app.config import get_settings
from app.models import ArbitrageOpportunity, NormalizedMarket, Venue
from app.risk import RiskManager


@pytest.fixture
def risk_fixture():
    settings = get_settings()
    settings.MAX_CONTRACTS_PER_LEG = 1
    settings.MAX_CONCURRENT_ARBITRAGES = 1
    settings.MAX_UNHEDGED_POSITION = 1
    risk = RiskManager(settings)

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
        opportunity_id="opp-123",
        market_window="2026-10-03T07:00Z-07:15Z",
        polymarket_market=poly,
        kalshi_market=kalshi,
        direction="BUY_POLY_YES_BUY_KALSHI_NO",
        yes_venue=Venue.POLYMARKET_US,
        no_venue=Venue.KALSHI,
        yes_ask=Decimal("0.50"),
        no_ask=Decimal("0.35"),
        target_quantity=1,
        raw_combined_cost=Decimal("0.85"),
        theoretical_gross_edge=Decimal("0.15"),
        poly_fee=Decimal("0.02"),
        kalshi_fee=Decimal("0.02"),
        total_fees=Decimal("0.04"),
        expected_slippage=Decimal("0.01"),
        latency_risk_buffer=Decimal("0.005"),
        execution_risk_buffer=Decimal("0.005"),
        unwind_risk_buffer=Decimal("0.01"),
        min_profit_reserve=Decimal("0.01"),
        conservative_net_edge=Decimal("0.05"),
        executable_max_yes_price=Decimal("0.51"),
        executable_max_no_price=Decimal("0.36"),
        is_executable=True,
    )

    return risk, opp


def test_kill_switch_blocks_orders(risk_fixture):
    risk, opp = risk_fixture
    risk.trading_mode = "PAPER"

    # Pre-check passes
    ok, _ = risk.validate_pre_execution(opp)
    assert ok is True

    # Activate kill switch
    risk.activate_kill_switch("Emergency test")
    ok_blocked, reason = risk.validate_pre_execution(opp)
    assert ok_blocked is False
    assert "kill switch" in reason.lower()

    # Reset
    risk.deactivate_kill_switch()
    ok_after, _ = risk.validate_pre_execution(opp)
    assert ok_after is True


def test_order_deduplication(risk_fixture):
    risk, opp = risk_fixture
    risk.trading_mode = "PAPER"

    # Record trade start
    risk.record_arbitrage_started(opp)

    # Duplicate submission for same opportunity or market window
    ok, reason = risk.validate_pre_execution(opp)
    assert ok is False
    assert "already executing" in reason.lower()

    # Finish trade
    risk.record_arbitrage_finished(opp.opportunity_id, opp.market_window)
    ok_after, _ = risk.validate_pre_execution(opp)
    assert ok_after is True


def test_contract_quantity_limit(risk_fixture):
    risk, opp = risk_fixture
    risk.trading_mode = "PAPER"

    opp.target_quantity = 5  # Exceeds limit of 1
    ok, reason = risk.validate_pre_execution(opp)
    assert ok is False
    assert "exceeds max_contracts_per_leg" in reason.lower()


def test_unhedged_position_limit(risk_fixture):
    risk, opp = risk_fixture
    risk.trading_mode = "PAPER"

    # Simulate existing unhedged leg
    risk.current_unhedged_contracts = 1
    ok, reason = risk.validate_pre_execution(opp)
    assert ok is False
    assert "unhedged position limit reached" in reason.lower()
