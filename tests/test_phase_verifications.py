"""Phase 4 dedicated verification test suite:
1. Zero-strike tolerance ($0.00 difference and missing strike rejection)
2. Cross-venue quote skew (<= 500ms enforcement)
3. IOC fill safety and partial fill reconciliation
4. Positive EV guarantees under worst-case fill boundaries
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
import pytest

from app.arbitrage import ArbitrageCalculator, MAX_QUOTE_SKEW_MS
from app.config import Settings
from app.execution import ExecutionEngine
from app.market_matcher import are_markets_equivalent
from app.models import (
    ArbitrageOpportunity,
    ExecutionStatus,
    NormalizedMarket,
    OrderBookLevel,
    OrderBookState,
    OrderExecutionResult,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    Venue,
)
from app.portfolio import PortfolioManager
from app.risk import RiskManager


@pytest.fixture
def base_markets():
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=1)
    end = start + timedelta(minutes=15)

    poly = NormalizedMarket(
        venue=Venue.POLYMARKET_US,
        market_id="poly-btc-target",
        ticker="cpc-btc-updown-15m-now",
        title="Bitcoin Up/Down 15m",
        description="Bitcoin Up/Down 15m",
        underlying="BTC",
        market_type="15-minute",
        interval_start=start,
        interval_end=end,
        duration_seconds=900,
        outcome_yes_definition="BTC >= strike",
        outcome_no_definition="BTC < strike",
        reference_source="CF Benchmarks BRTI",
        reference_price=Decimal("85000.00"),
        strike=Decimal("85000.00"),
        settlement_method="CASH_SETTLED",
        payout=Decimal("1.00"),
        status="OPEN",
        discovered_at=now,
        fee_coefficient=Decimal("0.07"),
    )

    kalshi = NormalizedMarket(
        venue=Venue.KALSHI,
        market_id="KXBTC15M-TARGET",
        ticker="KXBTC15M-TARGET",
        title="Bitcoin Price >= 85000.00 in 15m",
        description="Bitcoin Price >= 85000.00 in 15m",
        underlying="BTC",
        market_type="15-minute",
        interval_start=start,
        interval_end=end,
        duration_seconds=900,
        outcome_yes_definition="BTC >= 85000.00",
        outcome_no_definition="BTC < 85000.00",
        reference_source="CF Benchmarks BRTI",
        reference_price=Decimal("85000.00"),
        strike=Decimal("85000.00"),
        settlement_method="CASH_SETTLED",
        payout=Decimal("1.00"),
        status="OPEN",
        discovered_at=now,
        fee_coefficient=Decimal("0.07"),
    )

    return poly, kalshi


# =========================================================================
# 1. Zero-Strike Tolerance Verification
# =========================================================================

def test_zero_strike_tolerance_exact_match(base_markets):
    poly, kalshi = base_markets
    poly.reference_price = Decimal("85000.00")
    kalshi.reference_price = Decimal("85000.00")

    result = are_markets_equivalent(poly, kalshi, strike_tolerance=Decimal("0.00"))
    assert result.is_equivalent is True
    assert result.status == "VERIFIED"


def test_zero_strike_tolerance_rejects_any_difference(base_markets):
    poly, kalshi = base_markets
    poly.reference_price = Decimal("85000.00")
    # Even a 1-cent difference must be rejected under $0.00 tolerance
    kalshi.reference_price = Decimal("85000.01")

    result = are_markets_equivalent(poly, kalshi, strike_tolerance=Decimal("0.00"))
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "strike price discrepancy" in result.reason.lower()


def test_zero_strike_tolerance_rejects_missing_poly_strike(base_markets):
    poly, kalshi = base_markets
    poly.reference_price = None
    poly.strike = None
    kalshi.reference_price = Decimal("85000.00")

    result = are_markets_equivalent(poly, kalshi, strike_tolerance=Decimal("0.00"))
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "missing confirmed strike" in result.reason.lower()


def test_zero_strike_tolerance_rejects_missing_kalshi_strike(base_markets):
    poly, kalshi = base_markets
    poly.reference_price = Decimal("85000.00")
    kalshi.reference_price = None
    kalshi.strike = None

    result = are_markets_equivalent(poly, kalshi, strike_tolerance=Decimal("0.00"))
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "missing confirmed strike" in result.reason.lower()


# =========================================================================
# 2. Cross-Venue Quote Skew Verification (<= 500ms)
# =========================================================================

def test_quote_skew_within_500ms_passes(base_markets):
    poly, kalshi = base_markets
    calc = ArbitrageCalculator(Settings())

    t0_ns = time.perf_counter_ns()
    # 300ms skew
    poly_ns = t0_ns
    kalshi_ns = t0_ns + 300_000_000

    poly_book = OrderBookState(
        venue=Venue.POLYMARKET_US,
        market_id=poly.market_id,
        ticker=poly.ticker,
        timestamp_ns=poly_ns,
        best_yes_ask=Decimal("0.50"),
        best_yes_ask_size=Decimal("10"),
    )
    kalshi_book = OrderBookState(
        venue=Venue.KALSHI,
        market_id=kalshi.market_id,
        ticker=kalshi.ticker,
        timestamp_ns=kalshi_ns,
        best_no_ask=Decimal("0.40"),
        best_no_ask_size=Decimal("10"),
    )

    opps = calc.evaluate_opportunity(poly, kalshi, poly_book, kalshi_book)
    opp = [o for o in opps if o.direction == "BUY_POLY_YES_BUY_KALSHI_NO"][0]

    assert opp.quote_skew_ms <= 500.0
    assert opp.is_executable is True


def test_quote_skew_exceeding_500ms_rejected(base_markets):
    poly, kalshi = base_markets
    calc = ArbitrageCalculator(Settings())

    t0_ns = time.perf_counter_ns()
    # 501ms skew
    poly_ns = t0_ns
    kalshi_ns = t0_ns + 501_000_000

    poly_book = OrderBookState(
        venue=Venue.POLYMARKET_US,
        market_id=poly.market_id,
        ticker=poly.ticker,
        timestamp_ns=poly_ns,
        best_yes_ask=Decimal("0.50"),
        best_yes_ask_size=Decimal("10"),
    )
    kalshi_book = OrderBookState(
        venue=Venue.KALSHI,
        market_id=kalshi.market_id,
        ticker=kalshi.ticker,
        timestamp_ns=kalshi_ns,
        best_no_ask=Decimal("0.40"),
        best_no_ask_size=Decimal("10"),
    )

    opps = calc.evaluate_opportunity(poly, kalshi, poly_book, kalshi_book)
    opp = [o for o in opps if o.direction == "BUY_POLY_YES_BUY_KALSHI_NO"][0]

    assert opp.quote_skew_ms > 500.0
    assert opp.is_executable is False
    assert "cross-venue quote skew" in opp.rejection_reason.lower()
    assert "exceeds 500ms limit" in opp.rejection_reason.lower()


# =========================================================================
# 3. IOC Fill Safety & Partial Fill Reconciliation
# =========================================================================

@pytest.mark.asyncio
async def test_partial_fill_reconciliation_and_targeted_ioc_unwind(base_markets, monkeypatch):
    poly, kalshi = base_markets
    settings = Settings()
    settings.PAPER_TRADING = True
    settings.LIVE_TRADING = False

    class DummyClient:
        async def cancel_order(self, *args, **kwargs):
            return {"status": "CANCELLED"}
        def is_authenticated(self):
            return True

    risk = RiskManager(settings)
    engine = ExecutionEngine(settings, risk, DummyClient(), DummyClient())

    opp = ArbitrageOpportunity(
        opportunity_id="opp-partial-1",
        market_window=f"{poly.interval_start.isoformat()}-{poly.interval_end.isoformat()}",
        polymarket_market=poly,
        kalshi_market=kalshi,
        direction="BUY_POLY_YES_BUY_KALSHI_NO",
        yes_venue=Venue.POLYMARKET_US,
        no_venue=Venue.KALSHI,
        yes_ask=Decimal("0.50"),
        no_ask=Decimal("0.40"),
        available_quantity=10,
        target_quantity=10,
        raw_combined_cost=Decimal("0.90"),
        theoretical_gross_edge=Decimal("0.10"),
        poly_fee=Decimal("0.035"),
        kalshi_fee=Decimal("0.02"),
        total_fees=Decimal("0.055"),
        expected_slippage=Decimal("0.01"),
        latency_risk_buffer=Decimal("0.005"),
        execution_risk_buffer=Decimal("0.005"),
        unwind_risk_buffer=Decimal("0.005"),
        min_profit_reserve=Decimal("0.01"),
        conservative_net_edge=Decimal("0.01"),
        expected_value=Decimal("0.015"),
        quote_skew_ms=50.0,
        executable_max_yes_price=Decimal("0.51"),
        executable_max_no_price=Decimal("0.41"),
        is_executable=True,
        rejection_reason=None,
        detected_at=datetime.now(timezone.utc),
        detected_monotonic_ns=time.perf_counter_ns(),
    )

    # Simulate asymmetrical partial fill: Polymarket fills 4 contracts, Kalshi fills 10 contracts
    async def mock_partial_fill(leg_yes_req, leg_no_req, opp):
        now_ns = time.perf_counter_ns()
        res_yes = OrderExecutionResult(
            venue=Venue.POLYMARKET_US,
            order_id="poly-part-1",
            client_order_id=leg_yes_req.client_order_id,
            opportunity_id=opp.opportunity_id,
            status=ExecutionStatus.FILLED,
            price=Decimal("0.50"),
            fill_price=Decimal("0.50"),
            quantity=10,
            fill_quantity=4,  # Partial fill: 4 contracts
            fee_paid=Decimal("0.14"),
            submitted_at_ns=now_ns,
            ack_at_ns=now_ns,
            filled_at_ns=now_ns,
        )
        res_no = OrderExecutionResult(
            venue=Venue.KALSHI,
            order_id="kalshi-part-1",
            client_order_id=leg_no_req.client_order_id,
            opportunity_id=opp.opportunity_id,
            status=ExecutionStatus.FILLED,
            price=Decimal("0.40"),
            fill_price=Decimal("0.40"),
            quantity=10,
            fill_quantity=10,  # Full fill: 10 contracts
            fee_paid=Decimal("0.20"),
            submitted_at_ns=now_ns,
            ack_at_ns=now_ns,
            filled_at_ns=now_ns,
        )
        return res_yes, res_no

    monkeypatch.setattr(engine, "_simulate_paper_execution", mock_partial_fill)

    trade = await engine.execute_arbitrage(opp)

    assert trade is not None
    assert trade.is_hedged is False
    # Orphan side is NO with orphan_qty = 10 - 4 = 6 contracts
    assert "EMERGENCY_UNWIND_IOC_NO_LEG_6X" in trade.recovery_action
    assert trade.recovery_result is not None
    assert trade.recovery_result.fill_quantity == 6
    assert trade.recovery_result.status == ExecutionStatus.FILLED

    # Unwind loss calculation:
    # 4 contracts hedged: (1.00 - 0.50 - 0.40) * 4 = +$0.40 gross
    # 6 contracts unwound at exit loss ($0.02 / contract) = -$0.12
    # Total fees = 0.14 + 0.20 + 0.01 = 0.35
    # Net PnL = 0.40 - 0.12 - 0.35 = -$0.07
    assert trade.realized_pnl == Decimal("-0.07")


# =========================================================================
# 4. Positive EV Guarantees Under Worst-Case Fill Boundaries
# =========================================================================

def test_worst_case_fill_boundary_preserves_positive_margin(base_markets):
    poly, kalshi = base_markets
    calc = ArbitrageCalculator(Settings())
    now_ns = time.perf_counter_ns()

    poly_book = OrderBookState(
        venue=Venue.POLYMARKET_US,
        market_id=poly.market_id,
        ticker=poly.ticker,
        timestamp_ns=now_ns,
        best_yes_ask=Decimal("0.48"),
        best_yes_ask_size=Decimal("20"),
    )
    kalshi_book = OrderBookState(
        venue=Venue.KALSHI,
        market_id=kalshi.market_id,
        ticker=kalshi.ticker,
        timestamp_ns=now_ns,
        best_no_ask=Decimal("0.42"),
        best_no_ask_size=Decimal("20"),
    )

    opps = calc.evaluate_opportunity(poly, kalshi, poly_book, kalshi_book)
    opp = [o for o in opps if o.direction == "BUY_POLY_YES_BUY_KALSHI_NO"][0]

    assert opp.is_executable is True
    assert opp.expected_value > Decimal("0.00")
    assert opp.conservative_net_edge >= Decimal("0.01")

    # Verify worst-case execution boundary constraint:
    # executable_max_yes_price + executable_max_no_price + total_fees <= 1.00 - MIN_PROFIT_RESERVE
    worst_case_cost = opp.executable_max_yes_price + opp.executable_max_no_price
    min_reserve = calc.settings.MIN_PROFIT_RESERVE
    max_allowable = Decimal("1.00") - opp.total_fees - min_reserve

    assert worst_case_cost <= max_allowable, (
        f"Worst-case fill {worst_case_cost} exceeds max allowable {max_allowable} "
        f"(MaxYes={opp.executable_max_yes_price}, MaxNo={opp.executable_max_no_price}, Fees={opp.total_fees})"
    )
