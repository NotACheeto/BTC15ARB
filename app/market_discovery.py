"""Automated continuous market discovery engine for BTC 15-minute contracts.

Discovers active and upcoming intervals on Polymarket US and Kalshi without hardcoded IDs.
Handles interval rollovers, expired markets, and fail-closed pairing validation.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import logging
import aiohttp
from dateutil import parser as date_parser

from app.config import Settings
from app.market_matcher import are_markets_equivalent
from app.models import MarketEquivalenceResult, NormalizedMarket, Venue

logger = logging.getLogger(__name__)


class MarketDiscoveryEngine:
    """Discovers, normalizes, and pairs current and upcoming BTC 15-minute markets."""

    def __init__(self, settings: Settings, http_session: aiohttp.ClientSession | None = None):
        self.settings = settings
        self._session = http_session
        self._owns_session = False

        # Discovered normalized markets
        self.polymarket_markets: dict[str, NormalizedMarket] = {}
        self.kalshi_markets: dict[str, NormalizedMarket] = {}

        # Validated active pairing: (polymarket_market, kalshi_market, equivalence_result)
        self.current_pair: tuple[NormalizedMarket, NormalizedMarket, MarketEquivalenceResult] | None = None
        self.upcoming_pairs: list[tuple[NormalizedMarket, NormalizedMarket, MarketEquivalenceResult]] = []
        self.last_discovery_time: datetime | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5.0),
                headers={"User-Agent": "BTC15ArbBot/1.0"}
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def fetch_polymarket_markets(self) -> list[NormalizedMarket]:
        """Fetch and normalize active BTC 15m markets from Polymarket US gateway."""
        session = await self._get_session()
        discovered: list[NormalizedMarket] = []
        events: list[dict[str, Any]] = []
        seen_event_slugs: set[str] = set()

        try:
            for query in ("crypto-updown-btc-15m", "BTC"):
                url = f"{self.settings.POLYMARKET_US_GATEWAY_URL}/v1/search?query={query}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for ev in data.get("events", []):
                            eslug = ev.get("slug") or ev.get("ticker") or ""
                            if eslug and eslug not in seen_event_slugs:
                                seen_event_slugs.add(eslug)
                                events.append(ev)

                for event in events:
                    series_slug = event.get("seriesSlug", "")
                    if series_slug != "btc-updown-15m" and "15m" not in event.get("ticker", "").lower():
                        continue

                    for m in event.get("markets", []):
                        terms = m.get("assetPriceTerms") or {}
                        horizon = terms.get("horizon", "")
                        asset_info = terms.get("asset") or {}
                        symbol = asset_info.get("symbol", "").lower()

                        if horizon != "15m" or symbol != "btc":
                            continue

                        # Parse timestamps
                        w_start_str = terms.get("windowStart") or m.get("startDate")
                        w_end_str = terms.get("windowEnd") or m.get("endDate")
                        if not w_start_str or not w_end_str:
                            continue

                        w_start = date_parser.isoparse(w_start_str).astimezone(timezone.utc)
                        w_end = date_parser.isoparse(w_end_str).astimezone(timezone.utc)

                        # Parse strike / priceToBeat
                        price_to_beat = None
                        ptb_obj = terms.get("priceToBeat")
                        if ptb_obj and isinstance(ptb_obj, dict) and "value" in ptb_obj:
                            try:
                                price_to_beat = Decimal(str(ptb_obj["value"]))
                            except Exception:
                                pass

                        fee_coeff = Decimal(str(m.get("feeCoefficient", "0.0695")))

                        normalized = NormalizedMarket(
                            venue=Venue.POLYMARKET_US,
                            market_id=str(m.get("id")),
                            ticker=m.get("slug", ""),
                            title=m.get("title") or m.get("question", "BTC Up or Down 15m"),
                            description=m.get("description", ""),
                            underlying="BTC",
                            market_type="15M_INTERVAL",
                            interval_start=w_start,
                            interval_end=w_end,
                            duration_seconds=int((w_end - w_start).total_seconds()),
                            outcome_yes_definition="BTC settlement >= price to beat at window end",
                            outcome_no_definition="BTC settlement < price to beat at window end",
                            reference_source=terms.get("indexSymbol") or "BRTI",
                            reference_price=price_to_beat,
                            strike=price_to_beat,
                            settlement_method="CF_BENCHMARKS_BRTI_60S_AVERAGE",
                            payout=Decimal("1.00"),
                            status="active" if m.get("active") and not m.get("closed") else "closed",
                            fee_coefficient=fee_coeff,
                            raw_metadata=m,
                        )
                        discovered.append(normalized)
                        self.polymarket_markets[normalized.ticker] = normalized

        except Exception as e:
            logger.error("Error discovering Polymarket US BTC 15m markets: %s", e)

        return discovered

    async def fetch_kalshi_markets(self) -> list[NormalizedMarket]:
        """Fetch and normalize active BTC 15m markets from Kalshi."""
        session = await self._get_session()
        # Use elections/public endpoint or standard API
        url = f"{self.settings.KALSHI_ELECTIONS_API_URL}/markets?series_ticker=KXBTC15M&status=open"
        discovered: list[NormalizedMarket] = []

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Kalshi market query returned HTTP %d", resp.status)
                    return []
                data = await resp.json()
                markets = data.get("markets", [])

                for m in markets:
                    ticker = m.get("ticker", "")
                    if not ticker.startswith("KXBTC15M"):
                        continue

                    open_time_str = m.get("open_time")
                    close_time_str = m.get("close_time")
                    if not open_time_str or not close_time_str:
                        continue

                    open_time = date_parser.isoparse(open_time_str).astimezone(timezone.utc)
                    close_time = date_parser.isoparse(close_time_str).astimezone(timezone.utc)

                    floor_strike = None
                    if m.get("floor_strike") is not None:
                        try:
                            floor_strike = Decimal(str(m["floor_strike"]))
                        except Exception:
                            pass

                    rules = m.get("rules_primary", "")
                    ref_source = "BRTI" if "BRTI" in rules or "CF Benchmarks" in rules else "CF_BENCHMARKS_BRTI"

                    normalized = NormalizedMarket(
                        venue=Venue.KALSHI,
                        market_id=ticker,
                        ticker=ticker,
                        title=m.get("title", "BTC price up in next 15 mins?"),
                        description=rules,
                        underlying="BTC",
                        market_type="15M_INTERVAL",
                        interval_start=open_time,
                        interval_end=close_time,
                        duration_seconds=int((close_time - open_time).total_seconds()),
                        outcome_yes_definition="BRTI 60s avg at close >= 60s avg at open",
                        outcome_no_definition="BRTI 60s avg at close < 60s avg at open",
                        reference_source=ref_source,
                        reference_price=floor_strike,
                        strike=floor_strike,
                        settlement_method="CF_BENCHMARKS_BRTI_60S_AVERAGE",
                        payout=Decimal("1.00"),
                        status="active" if m.get("status") == "active" else str(m.get("status")),
                        fee_coefficient=self.settings.KALSHI_TAKER_FEE_COEFF,
                        raw_metadata=m,
                    )
                    discovered.append(normalized)
                    self.kalshi_markets[normalized.ticker] = normalized

        except Exception as e:
            logger.error("Error discovering Kalshi BTC 15m markets: %s", e)

        return discovered

    async def update_active_and_upcoming_pairs(self) -> tuple[
        tuple[NormalizedMarket, NormalizedMarket, MarketEquivalenceResult] | None,
        list[tuple[NormalizedMarket, NormalizedMarket, MarketEquivalenceResult]]
    ]:
        """Discover both venues, correlate intervals, and validate equivalence."""
        poly_task = asyncio.create_task(self.fetch_polymarket_markets())
        kalshi_task = asyncio.create_task(self.fetch_kalshi_markets())
        poly_markets, kalshi_markets = await asyncio.gather(poly_task, kalshi_task)

        now_utc = datetime.now(timezone.utc)
        verified_pairs: list[tuple[NormalizedMarket, NormalizedMarket, MarketEquivalenceResult]] = []

        # Find matching intervals
        for pm in poly_markets:
            for km in kalshi_markets:
                # Basic window matching: start time matches
                if pm.interval_start == km.interval_start and pm.interval_end == km.interval_end:
                    result = are_markets_equivalent(pm, km)
                    if result.is_equivalent:
                        pm.validated_at = now_utc
                        km.validated_at = now_utc
                        verified_pairs.append((pm, km, result))
                    else:
                        logger.warning("Market pairing rejected: %s", result.reason)

        # Sort pairs by interval_start
        verified_pairs.sort(key=lambda pair: pair[0].interval_start)

        # Distinguish currently active interval vs upcoming intervals
        active_pair = None
        upcoming: list[tuple[NormalizedMarket, NormalizedMarket, MarketEquivalenceResult]] = []

        for pair in verified_pairs:
            p_market = pair[0]
            if p_market.interval_start <= now_utc < p_market.interval_end:
                if active_pair is None:
                    active_pair = pair
            elif p_market.interval_start > now_utc:
                upcoming.append(pair)

        self.current_pair = active_pair
        self.upcoming_pairs = upcoming
        self.last_discovery_time = now_utc

        if active_pair:
            logger.info(
                "Active BTC 15m Pair: Poly='%s' <-> Kalshi='%s' [%s - %s]",
                active_pair[0].ticker,
                active_pair[1].ticker,
                active_pair[0].interval_start.strftime("%H:%M"),
                active_pair[0].interval_end.strftime("%H:%M")
            )
        else:
            logger.info("No active verified BTC 15m market pairing found for current interval")

        return active_pair, upcoming
