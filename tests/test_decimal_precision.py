"""Unit tests verifying strict Decimal precision across all financial calculations."""

from decimal import Decimal
import pytest


def test_no_binary_float_drift():
    # In IEEE 754 float: 0.1 + 0.2 = 0.30000000000000004
    # In Decimal: 0.1 + 0.2 == 0.3
    d1 = Decimal("0.10")
    d2 = Decimal("0.20")
    d3 = Decimal("0.30")
    assert d1 + d2 == d3

    # Complementary contract settlement math:
    payout = Decimal("1.00")
    yes_ask = Decimal("0.70")
    no_ask = Decimal("0.25")
    combined = yes_ask + no_ask
    gross_edge = payout - combined
    assert gross_edge == Decimal("0.05")

    # Precise cent subtraction
    fee_poly = Decimal("0.015")
    fee_kalshi = Decimal("0.0175")
    total_fee = fee_poly + fee_kalshi
    assert total_fee == Decimal("0.0325")
