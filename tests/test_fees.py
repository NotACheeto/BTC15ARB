"""Unit tests for fee calculations using exact Decimal arithmetic."""

from decimal import Decimal
import pytest

from app.fees import (
    calculate_kalshi_taker_fee,
    calculate_polymarket_taker_fee,
    calculate_total_arb_fees,
)
from app.models import Venue


def test_kalshi_fee_parabolic_curve():
    # P = 0.50 -> 0.07 * 1 * 0.25 = 0.0175 -> ceil = 0.02
    fee_50 = calculate_kalshi_taker_fee(Decimal("0.50"), 1)
    assert fee_50 == Decimal("0.02")

    # P = 0.70 -> 0.07 * 1 * 0.21 = 0.0147 -> ceil = 0.02
    fee_70 = calculate_kalshi_taker_fee(Decimal("0.70"), 1)
    assert fee_70 == Decimal("0.02")

    # P = 0.90 -> 0.07 * 1 * 0.09 = 0.0063 -> ceil = 0.01
    fee_90 = calculate_kalshi_taker_fee(Decimal("0.90"), 1)
    assert fee_90 == Decimal("0.01")


def test_polymarket_fee_dynamic_coeff():
    fee = calculate_polymarket_taker_fee(Decimal("0.50"), 1, coeff=Decimal("0.0695"))
    assert fee >= Decimal("0.01")


def test_total_arb_fees_breakdown():
    p_fee, k_fee, total = calculate_total_arb_fees(
        yes_price=Decimal("0.60"),
        no_price=Decimal("0.35"),
        yes_venue=Venue.POLYMARKET_US,
        no_venue=Venue.KALSHI,
        quantity=1,
    )
    assert p_fee > Decimal("0.00")
    assert k_fee > Decimal("0.00")
    assert total == p_fee + k_fee
    assert isinstance(total, Decimal)
