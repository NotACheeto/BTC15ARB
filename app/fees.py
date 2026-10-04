"""Deterministic fee calculation engine using exact Decimal arithmetic.

Models official taker fee schedules for both Kalshi and Polymarket US.
Always rounds up (ROUND_CEILING) to remain strictly conservative.
Supports exact scaling cleanly across all target contract quantities.
"""

from decimal import Decimal, ROUND_CEILING
from app.models import Venue


ONE = Decimal("1.00")
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
DEFAULT_KALSHI_COEFF = Decimal("0.07")
DEFAULT_POLY_COEFF = Decimal("0.0695")


def calculate_kalshi_taker_fee(
    price: Decimal,
    count: int = 1,
    coeff: Decimal = DEFAULT_KALSHI_COEFF
) -> Decimal:
    """Calculate Kalshi official taker fee across target contract count.
    
    Formula: ceil(coeff * C * P * (1 - P)) rounded up to the nearest cent.
    Where P is contract price in [0.01, 0.99] and C is contract count.
    Minimum fee is 1 cent per order if matched.
    """
    if price <= ZERO or price >= ONE or count <= 0:
        return ZERO
    
    # Uncertainty factor P * (1 - P)
    uncertainty = price * (ONE - price)
    raw_fee = coeff * Decimal(count) * uncertainty
    
    # Round up to nearest cent (conservative)
    fee_cents = raw_fee.quantize(CENT, rounding=ROUND_CEILING)
    return max(CENT, fee_cents)


def calculate_kalshi_per_contract_fee(
    price: Decimal,
    coeff: Decimal = DEFAULT_KALSHI_COEFF
) -> Decimal:
    """Calculate Kalshi single-contract baseline taker fee."""
    return calculate_kalshi_taker_fee(price, count=1, coeff=coeff)


def calculate_polymarket_taker_fee(
    price: Decimal,
    count: int = 1,
    coeff: Decimal = DEFAULT_POLY_COEFF
) -> Decimal:
    """Calculate Polymarket US crypto 15-minute taker fee across target contract count.
    
    Polymarket 15m crypto taker fee uses dynamic fee coefficient (approx 0.0695 ~ 0.07).
    Formula: round(coeff * C * P * (1 - P), 4).
    For conservative edge deduction, rounds up to the nearest cent (minimum 1 cent floor).
    """
    if price <= ZERO or price >= ONE or count <= 0:
        return ZERO
    
    uncertainty = price * (ONE - price)
    raw_fee = coeff * Decimal(count) * uncertainty
    
    # Conservatively round up to nearest cent
    fee_cents = raw_fee.quantize(CENT, rounding=ROUND_CEILING)
    return max(CENT, fee_cents)


def calculate_polymarket_per_contract_fee(
    price: Decimal,
    coeff: Decimal = DEFAULT_POLY_COEFF
) -> Decimal:
    """Calculate Polymarket single-contract baseline taker fee."""
    return calculate_polymarket_taker_fee(price, count=1, coeff=coeff)


def calculate_total_arb_fees(
    yes_price: Decimal,
    no_price: Decimal,
    yes_venue: Venue,
    no_venue: Venue,
    quantity: int = 1,
    poly_coeff: Decimal = DEFAULT_POLY_COEFF,
    kalshi_coeff: Decimal = DEFAULT_KALSHI_COEFF
) -> tuple[Decimal, Decimal, Decimal]:
    """Calculate total fee breakdown for an arbitrage trade pair of specified quantity.
    
    Returns:
        (poly_fee, kalshi_fee, total_fee) in Decimal dollars for the given quantity.
    """
    if quantity <= 0:
        return ZERO, ZERO, ZERO

    if yes_venue == Venue.POLYMARKET_US:
        poly_fee = calculate_polymarket_taker_fee(yes_price, count=quantity, coeff=poly_coeff)
        kalshi_fee = calculate_kalshi_taker_fee(no_price, count=quantity, coeff=kalshi_coeff)
    else:
        kalshi_fee = calculate_kalshi_taker_fee(yes_price, count=quantity, coeff=kalshi_coeff)
        poly_fee = calculate_polymarket_taker_fee(no_price, count=quantity, coeff=poly_coeff)
        
    total_fee = poly_fee + kalshi_fee
    return poly_fee, kalshi_fee, total_fee


def calculate_per_contract_arb_fees(
    yes_price: Decimal,
    no_price: Decimal,
    yes_venue: Venue,
    no_venue: Venue,
    poly_coeff: Decimal = DEFAULT_POLY_COEFF,
    kalshi_coeff: Decimal = DEFAULT_KALSHI_COEFF
) -> tuple[Decimal, Decimal, Decimal]:
    """Calculate exact per-contract fee breakdown for an arbitrage trade pair."""
    return calculate_total_arb_fees(
        yes_price=yes_price,
        no_price=no_price,
        yes_venue=yes_venue,
        no_venue=no_venue,
        quantity=1,
        poly_coeff=poly_coeff,
        kalshi_coeff=kalshi_coeff,
    )
