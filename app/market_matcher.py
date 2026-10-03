"""Dedicated fail-closed market matching and validation engine.

Ensures that Polymarket US and Kalshi contracts represent the EXACT same economic event.
Structurally eliminates cross-market confusion (e.g., S&P 500 vs. BTC, or mismatched time intervals).
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from app.models import MarketEquivalenceResult, NormalizedMarket

logger = logging.getLogger(__name__)

import re

FORBIDDEN_ASSET_PATTERNS = [
    re.compile(r"\bS&P\b", re.IGNORECASE),
    re.compile(r"\bSPX\b", re.IGNORECASE),
    re.compile(r"\bINX\b", re.IGNORECASE),
    re.compile(r"\b500\b", re.IGNORECASE),
    re.compile(r"\bNASDAQ\b", re.IGNORECASE),
    re.compile(r"\bETH\b", re.IGNORECASE),
    re.compile(r"\bETHEREUM\b", re.IGNORECASE),
    re.compile(r"\bSOL\b", re.IGNORECASE),
    re.compile(r"\bSOLANA\b", re.IGNORECASE),
    re.compile(r"\bDOW\b", re.IGNORECASE),
    re.compile(r"\bDJIA\b", re.IGNORECASE),
]


def are_markets_equivalent(
    polymarket_market: NormalizedMarket,
    kalshi_market: NormalizedMarket,
    strike_tolerance: Decimal = Decimal("0.50"),  # Maximum allowable strike discrepancy in USD
) -> MarketEquivalenceResult:
    """Strict, deterministic, fail-closed equivalence verification.
    
    If ANY condition fails or is ambiguous, this function rejects the pairing.
    """
    details: dict[str, str] = {}

    # 1. Null / Type Safety Check
    if not polymarket_market or not kalshi_market:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason="Market object is null or missing",
            details={"error": "One or both market representations are None"}
        )

    details["polymarket_id"] = polymarket_market.market_id
    details["kalshi_id"] = kalshi_market.market_id
    details["polymarket_ticker"] = polymarket_market.ticker
    details["kalshi_ticker"] = kalshi_market.ticker

    # 2. Strict Underlying Asset Check
    poly_und = polymarket_market.underlying.strip().upper()
    kalshi_und = kalshi_market.underlying.strip().upper()

    if poly_und not in ("BTC", "BITCOIN"):
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Polymarket underlying '{poly_und}' is not Bitcoin (BTC)",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    if kalshi_und not in ("BTC", "BITCOIN"):
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Kalshi underlying '{kalshi_und}' is not Bitcoin (BTC)",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    # 3. Explicit Anti-Confusion Guard: Scan for forbidden assets (S&P 500, ETH, etc.)
    combined_text = (
        f"{polymarket_market.title} {polymarket_market.description} "
        f"{kalshi_market.title} {kalshi_market.description} "
        f"{polymarket_market.ticker} {kalshi_market.ticker}"
    ).upper()

    for pattern in FORBIDDEN_ASSET_PATTERNS:
        match = pattern.search(combined_text)
        if match:
            forbidden_matched = match.group(0)
            return MarketEquivalenceResult(
                is_equivalent=False,
                status="REJECTED",
                reason=f"Forbidden cross-asset identifier detected in metadata: '{forbidden_matched}'",
                polymarket_id=polymarket_market.market_id,
                kalshi_id=kalshi_market.market_id,
                details={"forbidden_token": forbidden_matched, **details},
            )

    # 4. Market Duration & Type Check
    if polymarket_market.duration_seconds != 900 or kalshi_market.duration_seconds != 900:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=(
                f"Duration mismatch: Polymarket={polymarket_market.duration_seconds}s, "
                f"Kalshi={kalshi_market.duration_seconds}s (Must be exactly 900s for 15m)"
            ),
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    # 5. Exact Time Window Alignment (Must match to the second)
    p_start = polymarket_market.interval_start
    k_start = kalshi_market.interval_start
    p_end = polymarket_market.interval_end
    k_end = kalshi_market.interval_end

    # Ensure UTC timezone awareness
    if p_start.tzinfo is None:
        p_start = p_start.replace(tzinfo=timezone.utc)
    if k_start.tzinfo is None:
        k_start = k_start.replace(tzinfo=timezone.utc)
    if p_end.tzinfo is None:
        p_end = p_end.replace(tzinfo=timezone.utc)
    if k_end.tzinfo is None:
        k_end = k_end.replace(tzinfo=timezone.utc)

    if p_start != k_start:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Interval start time mismatch: Poly={p_start.isoformat()} vs Kalshi={k_start.isoformat()}",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    if p_end != k_end:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Interval end time mismatch: Poly={p_end.isoformat()} vs Kalshi={k_end.isoformat()}",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    # 6. Expiration / Liveness Check (Window end must be in the future)
    now_utc = datetime.now(timezone.utc)
    if p_end <= now_utc:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Market interval has already closed/expired at {p_end.isoformat()}",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    # 7. Market Tradability / Active Status Check
    active_statuses = {"ACTIVE", "OPEN", "MARKET_STATUS_OPEN"}
    if polymarket_market.status.upper() not in active_statuses:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Polymarket market is not active: status='{polymarket_market.status}'",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    if kalshi_market.status.upper() not in active_statuses:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Kalshi market is not active: status='{kalshi_market.status}'",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    # 8. Settlement Oracle / Reference Source Alignment
    poly_source = polymarket_market.reference_source.upper()
    kalshi_source = kalshi_market.reference_source.upper()
    if "BRTI" not in poly_source and "CF_BENCHMARKS" not in poly_source:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Polymarket settlement oracle '{poly_source}' not recognized as CF Benchmarks BRTI",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    if "BRTI" not in kalshi_source and "CF_BENCHMARKS" not in kalshi_source:
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=f"Kalshi settlement oracle '{kalshi_source}' not recognized as CF Benchmarks BRTI",
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    # 9. Reference / Strike Price Consistency Check
    p_strike = polymarket_market.reference_price or polymarket_market.strike
    k_strike = kalshi_market.reference_price or kalshi_market.strike

    if p_strike is not None and k_strike is not None:
        strike_diff = abs(p_strike - k_strike)
        if strike_diff > strike_tolerance:
            return MarketEquivalenceResult(
                is_equivalent=False,
                status="REJECTED",
                reason=(
                    f"Strike / Reference price discrepancy exceeds tolerance: "
                    f"Poly=${p_strike} vs Kalshi=${k_strike} (Diff=${strike_diff} > ${strike_tolerance})"
                ),
                polymarket_id=polymarket_market.market_id,
                kalshi_id=kalshi_market.market_id,
                details={"poly_strike": str(p_strike), "kalshi_strike": str(k_strike), **details},
            )

    # 10. Contract Payout & Multiplier Check
    if polymarket_market.payout != Decimal("1.00") or kalshi_market.payout != Decimal("1.00"):
        return MarketEquivalenceResult(
            is_equivalent=False,
            status="REJECTED",
            reason=(
                f"Contract payout mismatch: Poly=${polymarket_market.payout}, "
                f"Kalshi=${kalshi_market.payout} (Must be exactly $1.00)"
            ),
            polymarket_id=polymarket_market.market_id,
            kalshi_id=kalshi_market.market_id,
            details=details,
        )

    # All multi-point structural checks passed
    details["strike"] = str(p_strike or k_strike)
    details["window"] = f"{p_start.isoformat()} to {p_end.isoformat()}"

    return MarketEquivalenceResult(
        is_equivalent=True,
        status="VERIFIED",
        reason="Markets verified equivalent: matching underlying (BTC), interval, CF Benchmarks BRTI oracle, and payout",
        polymarket_id=polymarket_market.market_id,
        kalshi_id=kalshi_market.market_id,
        details=details,
    )
