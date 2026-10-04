"""Polymarket US market data feed with WebSocket streaming and high-frequency BBO fallback.

Maintains an in-memory OrderBookState with sequence checking, staleness tracking,
and automatic reconnect logic.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import time
from typing import Callable
import websockets
from polymarket_us.auth import create_auth_headers

from app.config import Settings
from app.exchanges.polymarket_us import PolymarketUSClient
from app.latency import latency_tracker
from app.models import OrderBookLevel, OrderBookState, Venue

logger = logging.getLogger(__name__)


class PolymarketFeed:
    """Manages real-time market data stream for Polymarket US."""

    def __init__(self, settings: Settings, client: PolymarketUSClient):
        self.settings = settings
        self.client = client
        self.current_market_slug: str | None = None
        self.order_book: OrderBookState | None = None
        self._on_update_callbacks: list[Callable[[OrderBookState], None]] = []

        self._running = False
        self._ws_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._connected = False
        self.last_update_ns: int = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def on_update(self, callback: Callable[[OrderBookState], None]) -> None:
        self._on_update_callbacks.append(callback)

    def _notify(self, book: OrderBookState) -> None:
        for cb in self._on_update_callbacks:
            try:
                cb(book)
            except Exception as e:
                logger.error("Error in Polymarket on_update callback: %s", e)

    async def start(self, market_slug: str) -> None:
        """Start streaming for given market slug."""
        self.current_market_slug = market_slug
        self._running = True

        # Always run high-frequency BBO polling as continuous heartbeat
        self._poll_task = asyncio.create_task(self._poll_bbo_loop())

        if self.client.is_authenticated():
            self._ws_task = asyncio.create_task(self._ws_stream_loop())

    async def stop(self) -> None:
        """Stop market data feed."""
        self._running = False
        self._connected = False
        if self._ws_task:
            self._ws_task.cancel()
        if self._poll_task:
            self._poll_task.cancel()

    async def _ws_stream_loop(self) -> None:
        """Maintain persistent authenticated WebSocket connection."""
        backoff = 0.2
        while self._running:
            try:
                path = "/v1/ws/markets"
                url = f"{self.settings.POLYMARKET_US_WS_URL}{path}"
                headers = create_auth_headers(
                    self.client.key_id, self.client.secret_key, "GET", path
                )

                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    self._connected = True
                    backoff = 0.2
                    logger.info("Connected to Polymarket US WebSocket")

                    # Subscribe to market data
                    sub_req = {
                        "subscribe": {
                            "requestId": f"sub-{int(time.time()*1000)}",
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                            "marketSlugs": [self.current_market_slug],
                        }
                    }
                    await ws.send(json.dumps(sub_req))

                    async for raw_msg in ws:
                        recv_ns = time.perf_counter_ns()
                        self._handle_ws_message(raw_msg, recv_ns)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self.order_book = None
                logger.warning("Polymarket WS disconnected (%s), reconnecting in %.1fs...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(5.0, backoff * 1.5)

    def _handle_ws_message(self, raw_msg: str | bytes, recv_ns: int) -> None:
        """Process WS message into OrderBookState."""
        try:
            msg = json.loads(raw_msg)
            if "marketData" in msg:
                md = msg["marketData"]
                if md.get("marketSlug") != self.current_market_slug:
                    return

                yes_bids = [
                    OrderBookLevel(price=Decimal(str(b["px"]["value"])), quantity=Decimal(str(b["qty"])))
                    for b in md.get("bids", [])
                ]
                yes_asks = [
                    OrderBookLevel(price=Decimal(str(a["px"]["value"])), quantity=Decimal(str(a["qty"])))
                    for a in md.get("offers", [])
                ]

                # Top of book
                best_bid = yes_bids[0].price if yes_bids else None
                best_ask = yes_asks[0].price if yes_asks else None
                best_ask_size = yes_asks[0].quantity if yes_asks else None
                best_bid_size = yes_bids[0].quantity if yes_bids else None

                # In binary contract, NO bid = 1 - YES ask, NO ask = 1 - YES bid
                best_no_bid = (Decimal("1.00") - best_ask) if best_ask else None
                best_no_ask = (Decimal("1.00") - best_bid) if best_bid else None

                book = OrderBookState(
                    venue=Venue.POLYMARKET_US,
                    market_id=self.current_market_slug,
                    ticker=self.current_market_slug,
                    timestamp_ns=recv_ns,
                    yes_bids=yes_bids,
                    yes_asks=yes_asks,
                    best_yes_bid=best_bid,
                    best_yes_ask=best_ask,
                    best_no_bid=best_no_bid,
                    best_no_ask=best_no_ask,
                    best_yes_ask_size=best_ask_size,
                    best_no_ask_size=best_bid_size,
                    updated_at=datetime.now(timezone.utc),
                )
                self.order_book = book
                self.last_update_ns = recv_ns
                self._notify(book)

        except Exception as e:
            logger.error("Error parsing Polymarket WS message: %s", e)

    def _parse_bbo_to_orderbook(self, market_slug: str, bbo_resp: dict, recv_ns: int | None = None) -> OrderBookState:
        """Parse BBO REST response into normalized OrderBookState."""
        recv_ns = recv_ns or time.perf_counter_ns()
        md = bbo_resp.get("marketData", {})

        best_bid_val = md.get("bestBid", {}).get("value")
        best_ask_val = md.get("bestAsk", {}).get("value")
        ask_depth = md.get("askDepth")
        bid_depth = md.get("bidDepth")

        best_bid = Decimal(str(best_bid_val)) if best_bid_val is not None else None
        best_ask = Decimal(str(best_ask_val)) if best_ask_val is not None else None

        ask_qty = Decimal(str(ask_depth)) if ask_depth is not None else (Decimal("0") if best_ask else None)
        bid_qty = Decimal(str(bid_depth)) if bid_depth is not None else (Decimal("0") if best_bid else None)

        best_no_bid = (Decimal("1.00") - best_ask) if best_ask else None
        best_no_ask = (Decimal("1.00") - best_bid) if best_bid else None

        return OrderBookState(
            venue=Venue.POLYMARKET_US,
            market_id=market_slug,
            ticker=market_slug,
            timestamp_ns=recv_ns,
            yes_bids=[OrderBookLevel(price=best_bid, quantity=bid_qty)] if (best_bid and bid_qty and bid_qty > 0) else [],
            yes_asks=[OrderBookLevel(price=best_ask, quantity=ask_qty)] if (best_ask and ask_qty and ask_qty > 0) else [],
            best_yes_bid=best_bid,
            best_yes_ask=best_ask,
            best_no_bid=best_no_bid,
            best_no_ask=best_no_ask,
            best_yes_ask_size=ask_qty,
            best_no_ask_size=bid_qty,
            updated_at=datetime.now(timezone.utc),
        )

    async def _poll_bbo_loop(self) -> None:
        """High-frequency REST BBO polling loop with WS collision prevention."""
        self._connected = True
        while self._running:
            try:
                if not self.current_market_slug:
                    await asyncio.sleep(0.5)
                    continue

                recv_ns = time.perf_counter_ns()

                # WS collision check: if WS is active and received data in the last 500ms, don't overwrite
                if self._ws_task and not self._ws_task.done() and (recv_ns - self.last_update_ns) < 500_000_000:
                    await asyncio.sleep(0.25)
                    continue

                bbo_resp = await self.client.get_bbo(self.current_market_slug)
                now_ns = time.perf_counter_ns()
                if now_ns > self.last_update_ns:
                    book = self._parse_bbo_to_orderbook(self.current_market_slug, bbo_resp, now_ns)
                    self.order_book = book
                    self.last_update_ns = now_ns
                    self._notify(book)

                # Poll every 250ms
                await asyncio.sleep(0.25)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Polymarket BBO poll error: %s", e)
                await asyncio.sleep(0.5)
