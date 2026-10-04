"""Unit tests for the conservative arbitrage edge and risk haircut engine."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
import pytest

from app.arbitrage import ArbitrageCalculator
from app.config import get_settings
from app.models import NormalizedMarket, OrderBookLevel, OrderBookState, Venue


@pytest.fixture
def calc_fixture():
    settings = get_settings()
    settings.MIN_NET_EDGE = Decimal("0.02")
    calc = ArbitrageCalculator(settings)

    now = datetime.now(timezone.utc)
    t_start = now + timedelta(minutes=2)
    t_end = t_start + timedelta(minutes=15)

    poly = NormalizedMarket(
        venue=Venue.POLYMARKET_US,
        market_id="p-1",
        ticker="poly-test",
        title="BTC 15m",
        description="BTC",
        underlying="BTC",
        market_type="15M_INTERVAL",
        interval_start=t_start,
        interval_end=t_end,
        duration_seconds=900,
        outcome_yes_definition="Settlement >= strike",
        outcome_no_definition="Settlement < strike",
        reference_source="BRTI",
        reference_price=Decimal("84000.00"),
        settlement_method="BRTI",
        payout=Decimal("1.00"),
        status="active",
    )

    kalshi = NormalizedMarket(
        venue=Venue.KALSHI,
        market_id="k-1",
        ticker="kalshi-test",
        title="BTC 15m",
        description="BTC",
        underlying="BTC",
        market_type="15M_INTERVAL",
        interval_start=t_start,
        interval_end=t_end,
        duration_seconds=900,
        outcome_yes_definition="Settlement >= strike",
        outcome_no_definition="Settlement < strike",
        reference_source="BRTI",
        reference_price=Decimal("84000.00"),
        settlement_method="BRTI",
        payout=Decimal("1.00"),
        status="active",
    )

    return calc, poly, kalshi


def test_positive_executable_edge(calc_fixture):
    calc, poly, kalshi = calc_fixture
    now_ns = time.perf_counter_ns()

    # Poly YES Ask = $0.50, Kalshi NO Ask = $0.35 -> Combined = $0.85
    # Gross edge = $0.15 (15 cents!)
    # Even after fees (~0.04), slippage (0.01), latency (0.005), unwind (0.01), reserve (0.01)
    # Net edge should be roughly 15c - ~7.5c = ~7.5c > 2c threshold!
    poly_book = OrderBookState(
        venue=Venue.POLYMARKET_US,
        market_id="p-1",
        ticker="poly-test",
        timestamp_ns=now_ns,
        best_yes_ask=Decimal("0.50"),
        best_no_ask=Decimal("0.52"),
        best_yes_ask_size=Decimal("10"),
        best_no_ask_size=Decimal("10"),
    )

    kalshi_book = OrderBookState(
        venue=Venue.KALSHI,
        market_id="k-1",
        ticker="kalshi-test",
        timestamp_ns=now_ns,
        best_yes_ask=Decimal("0.66"),
        best_no_ask=Decimal("0.35"),
        best_yes_ask_size=Decimal("10"),
        best_no_ask_size=Decimal("10"),
    )

    opps = calc.evaluate_opportunity(poly, kalshi, poly_book, kalshi_book)
    opp1 = [o for o in opps if o.direction == "BUY_POLY_YES_BUY_KALSHI_NO"][0]

    assert opp1.raw_combined_cost == Decimal("0.85")
    assert opp1.theoretical_gross_edge == Decimal("0.15")
    assert opp1.conservative_net_edge > Decimal("0.02")
    assert opp1.is_executable is True
    assert opp1.rejection_reason is None


def test_zero_or_negative_edge_rejected(calc_fixture):
    calc, poly, kalshi = calc_fixture
    now_ns = time.perf_counter_ns()

    # Poly YES Ask = 0.60, Kalshi NO Ask = 0.45 -> Combined = 1.05 -> Negative theoretical edge!
    poly_book = OrderBookState(
        venue=Venue.POLYMARKET_US,
        market_id="p-1",
        ticker="poly-test",
        timestamp_ns=now_ns,
        best_yes_ask=Decimal("0.60"),
        best_no_ask=Decimal("0.42"),
        best_yes_ask_size=Decimal("10"),
    )

    kalshi_book = OrderBookState(
        venue=Venue.KALSHI,
        market_id="k-1",
        ticker="kalshi-test",
        timestamp_ns=now_ns,
        best_yes_ask=Decimal("0.58"),
        best_no_ask=Decimal("0.45"),
        best_no_ask_size=Decimal("10"),
    )

    opps = calc.evaluate_opportunity(poly, kalshi, poly_book, kalshi_book)
    opp1 = [o for o in opps if o.direction == "BUY_POLY_YES_BUY_KALSHI_NO"][0]

    assert opp1.raw_combined_cost == Decimal("1.05")
    assert opp1.theoretical_gross_edge == Decimal("-0.05")
    assert opp1.is_executable is False
    assert "negative" in opp1.rejection_reason.lower()


def test_edge_haircut_absorbs_thin_edge(calc_fixture):
    calc, poly, kalshi = calc_fixture
    now_ns = time.perf_counter_ns()

    # Poly YES Ask = 0.58, Kalshi NO Ask = 0.39 -> Combined = 0.97
    # Gross edge = 0.03 (3 cents)
    # But fees (~4c) + slippage + latency buffer consume all 3 cents -> Conservative net edge < 0!
    poly_book = OrderBookState(
        venue=Venue.POLYMARKET_US,
        market_id="p-1",
        ticker="poly-test",
        timestamp_ns=now_ns,
        best_yes_ask=Decimal("0.58"),
        best_no_ask=Decimal("0.44"),
        best_yes_ask_size=Decimal("10"),
    )

    kalshi_book = OrderBookState(
        venue=Venue.KALSHI,
        market_id="k-1",
        ticker="kalshi-test",
        timestamp_ns=now_ns,
        best_yes_ask=Decimal("0.62"),
        best_no_ask=Decimal("0.39"),
        best_no_ask_size=Decimal("10"),
    )

    opps = calc.evaluate_opportunity(poly, kalshi, poly_book, kalshi_book)
    opp1 = [o for o in opps if o.direction == "BUY_POLY_YES_BUY_KALSHI_NO"][0]

    assert opp1.theoretical_gross_edge == Decimal("0.03")
    assert opp1.conservative_net_edge < Decimal("0.00")
    assert opp1.is_executable is False
    assert (
        "below min_net_edge" in opp1.rejection_reason.lower()
        or "non-positive" in opp1.rejection_reason.lower()
    )


def test_stale_quote_rejected(calc_fixture):
    calc, poly, kalshi = calc_fixture
    now_ns = time.perf_counter_ns()
    # 5 seconds old quote (stale)
    stale_ns = now_ns - (5000 * 1_000_000)

    poly_book = OrderBookState(
        venue=Venue.POLYMARKET_US,
        market_id="p-1",
        ticker="poly-test",
        timestamp_ns=stale_ns,
        best_yes_ask=Decimal("0.50"),
        best_yes_ask_size=Decimal("10"),
    )

    kalshi_book = OrderBookState(
        venue=Venue.KALSHI,
        market_id="k-1",
        ticker="kalshi-test",
        timestamp_ns=now_ns,
        best_no_ask=Decimal("0.30"),
        best_no_ask_size=Decimal("10"),
    )

    opps = calc.evaluate_opportunity(poly, kalshi, poly_book, kalshi_book)
    opp1 = [o for o in opps if o.direction == "BUY_POLY_YES_BUY_KALSHI_NO"][0]

    assert opp1.is_executable is False
    assert "stale" in opp1.rejection_reason.lower()
