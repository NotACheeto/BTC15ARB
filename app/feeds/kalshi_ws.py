"""Kalshi market data feed with WebSocket streaming and high-frequency orderbook fallback.

Ingests orderbook snapshots and deltas, maintains local bid ladders, and derives
executable YES/NO asks using Kalshi's bids-only contract equivalence.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import time
from typing import Callable
import websockets

from app.config import Settings
from app.exchanges.kalshi import KalshiClient
from app.latency import latency_tracker
from app.models import OrderBookLevel, OrderBookState, Venue

logger = logging.getLogger(__name__)


class KalshiFeed:
    """Manages real-time market data stream for Kalshi."""

    def __init__(self, settings: Settings, client: KalshiClient):
        self.settings = settings
        self.client = client
        self.current_ticker: str | None = None
        self.order_book: OrderBookState | None = None
        self._on_update_callbacks: list[Callable[[OrderBookState], None]] = []
        self._yes_bids: dict[Decimal, Decimal] = {}
        self._no_bids: dict[Decimal, Decimal] = {}

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
                logger.error("Error in Kalshi on_update callback: %s", e)

    async def start(self, ticker: str) -> None:
        """Start streaming market data for ticker."""
        self.current_ticker = ticker
        self._running = True

        # Always start REST orderbook polling as heartbeat
        self._poll_task = asyncio.create_task(self._poll_orderbook_loop())

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
        """Maintain persistent authenticated WebSocket connection to Kalshi."""
        backoff = 0.2
        while self._running:
            try:
                url = self.settings.KALSHI_WS_URL
                headers = self.client._create_auth_headers("GET", "/trade-api/ws/v2")

                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    self._connected = True
                    backoff = 0.2
                    logger.info("Connected to Kalshi WebSocket")

                    # Subscribe to orderbook_delta
                    sub_req = {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": [self.current_ticker],
                        },
                    }
                    await ws.send(json.dumps(sub_req))

                    async for raw_msg in ws:
                        recv_ns = time.perf_counter_ns()
                        self._handle_ws_message(raw_msg, recv_ns)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.warning("Kalshi WS disconnected (%s), reconnecting in %.1fs...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(5.0, backoff * 1.5)

    def _handle_ws_message(self, raw_msg: str | bytes, recv_ns: int) -> None:
        """Handle snapshot and delta messages from Kalshi WS."""
        try:
            msg = json.loads(raw_msg)
            msg_type = msg.get("type")
            payload = msg.get("msg", {})

            if payload.get("market_ticker") != self.current_ticker:
                return

            if msg_type == "orderbook_snapshot":
                self._apply_snapshot(payload, recv_ns)
            elif msg_type == "orderbook_delta":
                self._apply_delta(payload, recv_ns)
        except Exception as e:
            logger.error("Error parsing Kalshi WS message: %s", e)

    def _apply_snapshot(self, payload: dict, recv_ns: int) -> None:
        """Construct OrderBookState from Kalshi fixed-point snapshot."""
        yes_bids_raw = (
            payload.get("yes_dollars")
            or payload.get("yes_dollars_fp")
            or payload.get("yes")
            or []
        )
        no_bids_raw = (
            payload.get("no_dollars")
            or payload.get("no_dollars_fp")
            or payload.get("no")
            or []
        )

        self._yes_bids = {Decimal(str(p)): Decimal(str(s)) for p, s in yes_bids_raw}
        self._no_bids = {Decimal(str(p)): Decimal(str(s)) for p, s in no_bids_raw}
        self._rebuild_book(recv_ns)

    def _apply_delta(self, payload: dict, recv_ns: int) -> None:
        """Apply delta to internal book and rebuild OrderBookState."""
        price_val = payload.get("price_dollars")
        delta_val = payload.get("delta_fp")
        side = payload.get("side", "").lower()

        if price_val is not None and delta_val is not None:
            px = Decimal(str(price_val))
            delta = Decimal(str(delta_val))
            target_dict = self._yes_bids if side == "yes" else self._no_bids
            new_qty = target_dict.get(px, Decimal("0")) + delta
            if new_qty > Decimal("0"):
                target_dict[px] = new_qty
            else:
                target_dict.pop(px, None)
            self._rebuild_book(recv_ns)

    def _rebuild_book(self, recv_ns: int) -> None:
        """Reconstruct OrderBookState from internal bid ladders."""
        yes_bids = [
            OrderBookLevel(price=p, quantity=s)
            for p, s in self._yes_bids.items()
        ]
        no_bids = [
            OrderBookLevel(price=p, quantity=s)
            for p, s in self._no_bids.items()
        ]

        # Top of book
        best_yes_bid = max([lvl.price for lvl in yes_bids], default=None)
        best_no_bid = max([lvl.price for lvl in no_bids], default=None)

        # In Kalshi bids-only model:
        # Best YES Ask = 1 - Best NO Bid
        best_yes_ask = (Decimal("1.00") - best_no_bid) if best_no_bid else None
        # Best NO Ask = 1 - Best YES Bid
        best_no_ask = (Decimal("1.00") - best_yes_bid) if best_yes_bid else None

        # Sizes at best asks
        best_yes_ask_size = Decimal("100")
        best_no_ask_size = Decimal("100")
        for lvl in no_bids:
            if lvl.price == best_no_bid:
                best_yes_ask_size = lvl.quantity
                break
        for lvl in yes_bids:
            if lvl.price == best_yes_bid:
                best_no_ask_size = lvl.quantity
                break

        book = OrderBookState(
            venue=Venue.KALSHI,
            market_id=self.current_ticker,
            ticker=self.current_ticker,
            timestamp_ns=recv_ns,
            yes_bids=sorted(yes_bids, key=lambda x: x.price, reverse=True),
            no_bids=sorted(no_bids, key=lambda x: x.price, reverse=True),
            best_yes_bid=best_yes_bid,
            best_yes_ask=best_yes_ask,
            best_no_bid=best_no_bid,
            best_no_ask=best_no_ask,
            best_yes_ask_size=best_yes_ask_size,
            best_no_ask_size=best_no_ask_size,
            updated_at=datetime.now(timezone.utc),
        )
        self.order_book = book
        self.last_update_ns = recv_ns
        self._notify(book)

    async def _poll_orderbook_loop(self) -> None:
        """High-frequency REST order book polling loop."""
        self._connected = True
        while self._running:
            try:
                if not self.current_ticker:
                    await asyncio.sleep(0.5)
                    continue

                recv_ns = time.perf_counter_ns()
                raw_book = await self.client.get_orderbook(self.current_ticker)
                ob_data = raw_book.get("orderbook_fp") or raw_book.get("orderbook") or raw_book
                self._apply_snapshot(ob_data, recv_ns)

                await asyncio.sleep(0.25)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Kalshi orderbook poll error: %s", e)
                await asyncio.sleep(0.5)
