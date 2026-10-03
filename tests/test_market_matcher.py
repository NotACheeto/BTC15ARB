"""Unit tests for the fail-closed market matching and validation engine.

Ensures strict structural rejection of S&P 500, wrong intervals, mismatched oracles,
mismatched strikes, and expired intervals.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from app.market_matcher import are_markets_equivalent
from app.models import NormalizedMarket, Venue


@pytest.fixture
def base_btc_markets():
    now = datetime.now(timezone.utc)
    t_start = now + timedelta(minutes=2)
    t_end = t_start + timedelta(minutes=15)

    poly = NormalizedMarket(
        venue=Venue.POLYMARKET_US,
        market_id="poly-btc-valid",
        ticker="cpc-btc-updown-15m-2026-10-03-0700z",
        title="BTC Up or Down 15m",
        description="Bitcoin CF Benchmarks BRTI 60s average",
        underlying="BTC",
        market_type="15M_INTERVAL",
        interval_start=t_start,
        interval_end=t_end,
        duration_seconds=900,
        outcome_yes_definition="Settlement >= strike",
        outcome_no_definition="Settlement < strike",
        reference_source="BRTI",
        reference_price=Decimal("84559.59"),
        settlement_method="60S_BRTI_AVERAGE",
        payout=Decimal("1.00"),
        status="active",
    )

    kalshi = NormalizedMarket(
        venue=Venue.KALSHI,
        market_id="KXBTC15M-26OCT030315-15",
        ticker="KXBTC15M-26OCT030315-15",
        title="BTC price up in next 15 mins?",
        description="CF Benchmarks BRTI 60s average before close",
        underlying="BTC",
        market_type="15M_INTERVAL",
        interval_start=t_start,
        interval_end=t_end,
        duration_seconds=900,
        outcome_yes_definition="BRTI >= open strike",
        outcome_no_definition="BRTI < open strike",
        reference_source="BRTI",
        reference_price=Decimal("84559.59"),
        settlement_method="60S_BRTI_AVERAGE",
        payout=Decimal("1.00"),
        status="active",
    )

    return poly, kalshi


def test_matching_markets_verified(base_btc_markets):
    poly, kalshi = base_btc_markets
    result = are_markets_equivalent(poly, kalshi)
    assert result.is_equivalent is True
    assert result.status == "VERIFIED"
    assert "verified equivalent" in result.reason.lower()


def test_reject_sp500_underlying(base_btc_markets):
    poly, kalshi = base_btc_markets
    kalshi.underlying = "SP500"
    kalshi.title = "S&P 500 up in next 15m"

    result = are_markets_equivalent(poly, kalshi)
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "not bitcoin" in result.reason.lower() or "sp500" in result.reason.lower()


def test_reject_forbidden_asset_in_text(base_btc_markets):
    poly, kalshi = base_btc_markets
    kalshi.description = "Resolves to YES if S&P 500 rises relative to Bitcoin"

    result = are_markets_equivalent(poly, kalshi)
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "forbidden" in result.reason.lower()


def test_reject_mismatched_interval_timestamps(base_btc_markets):
    poly, kalshi = base_btc_markets
    # Mismatch start by 1 second
    kalshi.interval_start = poly.interval_start + timedelta(seconds=1)

    result = are_markets_equivalent(poly, kalshi)
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "start time mismatch" in result.reason.lower()


def test_reject_wrong_duration(base_btc_markets):
    poly, kalshi = base_btc_markets
    # 1 hour instead of 15 min
    poly.duration_seconds = 3600

    result = are_markets_equivalent(poly, kalshi)
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "duration mismatch" in result.reason.lower()


def test_reject_expired_market(base_btc_markets):
    poly, kalshi = base_btc_markets
    # Set interval in the past
    poly.interval_end = datetime.now(timezone.utc) - timedelta(minutes=5)
    kalshi.interval_end = poly.interval_end

    result = are_markets_equivalent(poly, kalshi)
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "expired" in result.reason.lower() or "closed" in result.reason.lower()


def test_reject_mismatched_strikes(base_btc_markets):
    poly, kalshi = base_btc_markets
    poly.reference_price = Decimal("84500.00")
    kalshi.reference_price = Decimal("84600.00")  # $100 strike difference

    result = are_markets_equivalent(poly, kalshi, strike_tolerance=Decimal("0.50"))
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "discrepancy exceeds tolerance" in result.reason.lower()


def test_reject_mismatched_settlement_oracle(base_btc_markets):
    poly, kalshi = base_btc_markets
    kalshi.reference_source = "COINBASE_SPOT"

    result = are_markets_equivalent(poly, kalshi)
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "oracle" in result.reason.lower()


def test_reject_mismatched_payout(base_btc_markets):
    poly, kalshi = base_btc_markets
    kalshi.payout = Decimal("10.00")

    result = are_markets_equivalent(poly, kalshi)
    assert result.is_equivalent is False
    assert result.status == "REJECTED"
    assert "payout mismatch" in result.reason.lower()
