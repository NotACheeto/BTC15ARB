"""Configuration management for Polymarket US - Kalshi BTC Arbitrage Bot.

Prioritizes deterministic behavior, safe defaults, and strict validation.
"""

from decimal import Decimal
from typing import Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Trading Execution Modes (Default: LIVE OFF, PAPER ON)
    LIVE_TRADING: bool = Field(default=False, description="Master live trading switch. Must be explicitly enabled.")
    PAPER_TRADING: bool = Field(default=True, description="Simulate order fills with realistic fill model.")
    DATA_MODE: bool = Field(default=False, description="Read-only market data and matching inspection mode.")

    # Sizing & Position Limits (Initial conservative defaults: 1 contract per leg)
    MAX_CONTRACTS_PER_LEG: int = Field(default=1, ge=1, le=10, description="Max contracts per leg.")
    MAX_CONCURRENT_ARBITRAGES: int = Field(default=1, ge=1, le=5, description="Max concurrent active arbitrages.")
    MAX_UNHEDGED_POSITION: int = Field(default=1, ge=1, le=2, description="Max unhedged contracts permitted.")

    # Arbitrage Edge Thresholds
    MIN_NET_EDGE: Decimal = Field(
        default=Decimal("0.02"),
        description="Minimum conservative net edge required to trigger execution (in dollars, e.g. 0.02 = 2 cents)."
    )
    MAX_MARKET_DATA_AGE_MS: int = Field(
        default=1500,
        ge=100,
        le=10000,
        description="Maximum quote age in milliseconds before market data is considered stale."
    )
    MAX_ORPHAN_EXIT_LOSS: Decimal = Field(
        default=Decimal("0.02"),
        description="Max acceptable loss in dollars when unwinding an unhedged leg (e.g. 0.02 = 2 cents)."
    )

    # Execution & Latency Buffers
    EXECUTION_TIMEOUT_MS: int = Field(default=2000, description="Timeout for order submission and confirmation.")
    ORDER_CANCEL_TIMEOUT_MS: int = Field(default=1000, description="Timeout for order cancellation requests.")
    LATENCY_BUFFER_MODE: Literal["p50", "p90", "p95", "p99", "max"] = Field(
        default="p95",
        description="Percentile used for dynamic latency risk haircut."
    )
    LATENCY_PERCENTILE: float = Field(default=95.0, ge=50.0, le=100.0)

    # Sizing / Slippage Buffers
    DEFAULT_SLIPPAGE_BUFFER: Decimal = Field(
        default=Decimal("0.01"),
        description="Default execution slippage haircut in dollars."
    )
    MIN_PROFIT_RESERVE: Decimal = Field(
        default=Decimal("0.01"),
        description="Minimum profit reserve withheld before calculating executable edge."
    )
    UNWIND_RISK_BUFFER: Decimal = Field(
        default=Decimal("0.01"),
        description="Buffer reserved for potential unhedged leg liquidation cost."
    )

    # Fees configuration
    KALSHI_TAKER_FEE_COEFF: Decimal = Field(default=Decimal("0.07"))
    POLYMARKET_TAKER_FEE_COEFF: Decimal = Field(default=Decimal("0.0695"))

    # Polymarket US Credentials & Endpoints
    POLYMARKET_US_KEY_ID: str | None = Field(default=None)
    POLYMARKET_US_SECRET_KEY: str | None = Field(default=None)
    POLYMARKET_US_SECRET: str | None = Field(default=None)
    POLYMARKET_US_GATEWAY_URL: str = Field(default="https://gateway.polymarket.us")
    POLYMARKET_US_API_URL: str = Field(default="https://api.polymarket.us")
    POLYMARKET_US_WS_URL: str = Field(default="wss://api.polymarket.us")

    # Kalshi Credentials & Endpoints
    KALSHI_API_KEY_ID: str | None = Field(default=None)
    KALSHI_PRIVATE_KEY_PATH: str | None = Field(default=None)
    KALSHI_PRIVATE_KEY_PEM: str | None = Field(default=None)
    KALSHI_PRIVATE_KEY_CONTENT: str | None = Field(default=None)
    KALSHI_API_URL: str = Field(default="https://external-api.kalshi.com/trade-api/v2")
    KALSHI_ELECTIONS_API_URL: str = Field(default="https://api.elections.kalshi.com/trade-api/v2")
    KALSHI_WS_URL: str = Field(default="wss://external-api-ws.kalshi.com/trade-api/ws/v2")

    # Dashboard & Storage
    DASHBOARD_HOST: str = Field(default="127.0.0.1")
    DASHBOARD_PORT: int = Field(default=8050)
    DB_PATH: str = Field(default="btc_arbitrage.db")

    @field_validator("MIN_NET_EDGE", "MAX_ORPHAN_EXIT_LOSS", "DEFAULT_SLIPPAGE_BUFFER", "MIN_PROFIT_RESERVE", mode="before")
    @classmethod
    def parse_decimal(cls, value):
        if value is None:
            return value
        return Decimal(str(value))


def get_settings() -> Settings:
    """Return loaded settings singleton."""
    return Settings()
