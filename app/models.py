"""Core domain models and schemas for cross-exchange BTC 15-minute arbitrage.

Strictly enforces Decimal arithmetic for all prices, sizes, fees, and financial edges.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class Venue(str, Enum):
    POLYMARKET_US = "POLYMARKET_US"
    KALSHI = "KALSHI"


class OutcomeSide(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(str, Enum):
    IOC = "IOC"  # Immediate or Cancel
    FOK = "FOK"  # Fill or Kill
    GTC = "GTC"  # Good 'til Cancelled


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class NormalizedMarket(BaseModel):
    """Normalized cross-exchange representation of a BTC 15-minute contract."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    venue: Venue
    market_id: str
    ticker: str
    title: str
    description: str
    underlying: str  # Must normalize to "BTC"
    market_type: str  # e.g. "15M_INTERVAL"
    interval_start: datetime
    interval_end: datetime
    duration_seconds: int
    outcome_yes_definition: str
    outcome_no_definition: str
    reference_source: str  # e.g. "CF_BENCHMARKS_BRTI"
    reference_price: Decimal | None = None  # Opening strike / price to beat
    strike: Decimal | None = None
    settlement_method: str  # e.g. "60S_BRTI_AVERAGE"
    payout: Decimal = Decimal("1.00")
    status: str  # "active", "open", "closed", "halted"
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    validated_at: datetime | None = None
    fee_coefficient: Decimal = Decimal("0.07")
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class MarketEquivalenceResult(BaseModel):
    """Result of deterministic fail-closed market equivalence check."""
    is_equivalent: bool
    status: Literal["VERIFIED", "REJECTED"]
    reason: str
    polymarket_id: str | None = None
    kalshi_id: str | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = Field(default_factory=dict)


class OrderBookLevel(BaseModel):
    """Single price level in an order book."""
    price: Decimal
    quantity: Decimal


class OrderBookState(BaseModel):
    """Snapshot of a venue's executable order book."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    venue: Venue
    market_id: str
    ticker: str
    timestamp_ns: int = 0
    seq: int = 0
    is_stale: bool = False

    # Resting bids and asks
    yes_bids: list[OrderBookLevel] = Field(default_factory=list)
    yes_asks: list[OrderBookLevel] = Field(default_factory=list)
    no_bids: list[OrderBookLevel] = Field(default_factory=list)
    no_asks: list[OrderBookLevel] = Field(default_factory=list)

    # Top of book shortcuts
    best_yes_bid: Decimal | None = None
    best_yes_ask: Decimal | None = None
    best_no_bid: Decimal | None = None
    best_no_ask: Decimal | None = None

    best_yes_ask_size: Decimal | None = None
    best_no_ask_size: Decimal | None = None

    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ArbitrageOpportunity(BaseModel):
    """Fully calculated cross-exchange arbitrage opportunity with conservative edge decomposition."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    opportunity_id: str
    market_window: str  # e.g. "2026-10-03T07:00Z-07:15Z"
    polymarket_market: NormalizedMarket
    kalshi_market: NormalizedMarket

    # Direction: e.g. BUY_POLY_YES_BUY_KALSHI_NO or BUY_KALSHI_YES_BUY_POLY_NO
    direction: Literal["BUY_POLY_YES_BUY_KALSHI_NO", "BUY_KALSHI_YES_BUY_POLY_NO"]
    yes_venue: Venue
    no_venue: Venue

    # Executable prices from top-of-book
    yes_ask: Decimal
    no_ask: Decimal
    available_quantity: int = 1
    target_quantity: int = 1

    # Gross calculations
    payout: Decimal = Decimal("1.00")
    raw_combined_cost: Decimal
    theoretical_gross_edge: Decimal

    # Cost & risk deductions
    poly_fee: Decimal
    kalshi_fee: Decimal
    total_fees: Decimal
    expected_slippage: Decimal
    latency_risk_buffer: Decimal
    execution_risk_buffer: Decimal
    unwind_risk_buffer: Decimal
    min_profit_reserve: Decimal

    # Final conservative edge
    conservative_net_edge: Decimal

    # Maximum acceptable limit execution prices
    executable_max_yes_price: Decimal
    executable_max_no_price: Decimal

    # Execution eligibility
    is_executable: bool
    rejection_reason: str | None = None

    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    detected_monotonic_ns: int = 0


class OrderRequest(BaseModel):
    """Specification of an order to send to an exchange."""
    client_order_id: str
    opportunity_id: str
    venue: Venue
    market_id: str
    ticker: str
    side: OrderSide = OrderSide.BUY
    outcome: OutcomeSide
    price: Decimal
    quantity: int = 1
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.IOC
    created_at_ns: int = 0


class OrderExecutionResult(BaseModel):
    """Outcome of an order submission."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    venue: Venue
    order_id: str | None = None
    client_order_id: str
    opportunity_id: str
    status: ExecutionStatus
    price: Decimal
    fill_price: Decimal | None = None
    quantity: int = 1
    fill_quantity: int = 0
    fee_paid: Decimal = Decimal("0.00")

    submitted_at_ns: int = 0
    ack_at_ns: int = 0
    filled_at_ns: int = 0

    error_message: str | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)


class ArbitrageTradeRecord(BaseModel):
    """Persistent audit record of an executed or simulated arbitrage trade."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    trade_id: str
    opportunity_id: str
    mode: Literal["LIVE", "PAPER"]
    market_window: str
    direction: str

    # Leg 1 & Leg 2 results
    leg_yes: OrderExecutionResult
    leg_no: OrderExecutionResult

    is_hedged: bool
    recovery_action: str | None = None
    recovery_result: OrderExecutionResult | None = None

    total_cost: Decimal
    payout_expected: Decimal
    fees_paid: Decimal
    realized_pnl: Decimal

    latencies_ns: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RejectedOpportunityRecord(BaseModel):
    """Audit record of an observed opportunity rejected by the safety / EV model."""
    opportunity_id: str
    market_window: str
    direction: str
    raw_combined_cost: Decimal
    theoretical_gross_edge: Decimal
    conservative_net_edge: Decimal
    rejection_reason: str
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
