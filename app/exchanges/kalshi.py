"""High-performance asynchronous client for Kalshi (kalshi.com).

Supports RSA-PSS SHA-256 request signing, connection pooling,
orderbook fetching, order placement, positions, and cancellations.
"""

import base64
from decimal import Decimal
import time
from typing import Any
import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.config import Settings
from app.latency import latency_tracker
from app.models import ExecutionStatus, OrderExecutionResult, OrderRequest, Venue


class KalshiClient:
    """Async Kalshi trading and market data client."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None):
        self.settings = settings
        self.api_key_id = settings.KALSHI_API_KEY_ID
        self.api_url = settings.KALSHI_API_URL.rstrip("/")
        self.elections_api_url = settings.KALSHI_ELECTIONS_API_URL.rstrip("/")

        self._private_key = None
        self._session = session
        self._owns_session = False

        self._load_private_key()

    def _load_private_key(self) -> None:
        """Load RSA private key from PEM string or file path."""
        pem_data = None
        if self.settings.KALSHI_PRIVATE_KEY_PEM:
            pem_data = self.settings.KALSHI_PRIVATE_KEY_PEM.encode("utf-8")
        elif self.settings.KALSHI_PRIVATE_KEY_CONTENT:
            pem_data = self.settings.KALSHI_PRIVATE_KEY_CONTENT.encode("utf-8")
        elif self.settings.KALSHI_PRIVATE_KEY_PATH:
            try:
                with open(self.settings.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
                    pem_data = f.read()
            except Exception:
                pem_data = None

        if pem_data:
            try:
                self._private_key = load_pem_private_key(pem_data, password=None)
            except Exception:
                self._private_key = None

    def is_authenticated(self) -> bool:
        return bool(self.api_key_id and self._private_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=50, keepalive_timeout=60.0, enable_cleanup_closed=True)
            timeout = aiohttp.ClientTimeout(total=5.0, connect=2.0)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"User-Agent": "BTC15ArbBot/1.0", "Accept": "application/json"}
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def _create_auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Generate KALSHI-ACCESS-* RSA-PSS signature headers."""
        if not self.is_authenticated() or not self._private_key:
            return {"Content-Type": "application/json"}

        timestamp_str = str(int(time.time() * 1000))
        # Path strip query params for signing
        clean_path = path.split("?")[0]
        msg_str = f"{timestamp_str}{method.upper()}{clean_path}"

        signature = self._private_key.sign(
            msg_str.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key_id or "",
            "KALSHI-ACCESS-TIMESTAMP": timestamp_str,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """Fetch full order book for a market ticker."""
        session = await self._get_session()
        path = f"/trade-api/v2/markets/{ticker}/orderbook"
        url = f"{self.elections_api_url}/markets/{ticker}/orderbook"

        start_ns = time.perf_counter_ns()
        try:
            async with session.get(url) as resp:
                elapsed_ns = time.perf_counter_ns() - start_ns
                latency_tracker.kalshi_rest_rtt.record_ns(elapsed_ns)
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                raise RuntimeError(f"Kalshi get_orderbook failed HTTP {resp.status}: {text}")
        except Exception as e:
            elapsed_ns = time.perf_counter_ns() - start_ns
            latency_tracker.kalshi_rest_rtt.record_ns(elapsed_ns)
            raise e

    async def get_balance(self) -> dict[str, Any]:
        """Fetch portfolio balance."""
        if not self.is_authenticated():
            raise RuntimeError("Kalshi credentials not configured")

        session = await self._get_session()
        path = "/trade-api/v2/portfolio/balance"
        url = f"{self.api_url}/portfolio/balance"
        headers = self._create_auth_headers("GET", path)

        start_ns = time.perf_counter_ns()
        try:
            async with session.get(url, headers=headers) as resp:
                elapsed_ns = time.perf_counter_ns() - start_ns
                latency_tracker.kalshi_rest_rtt.record_ns(elapsed_ns)
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                raise RuntimeError(f"Kalshi get_balance failed HTTP {resp.status}: {text}")
        except Exception as e:
            elapsed_ns = time.perf_counter_ns() - start_ns
            latency_tracker.kalshi_rest_rtt.record_ns(elapsed_ns)
            raise e

    async def get_positions(self) -> dict[str, Any]:
        """Fetch user positions."""
        if not self.is_authenticated():
            raise RuntimeError("Kalshi credentials not configured")

        session = await self._get_session()
        path = "/trade-api/v2/portfolio/positions"
        url = f"{self.api_url}/portfolio/positions"
        headers = self._create_auth_headers("GET", path)

        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            text = await resp.text()
            raise RuntimeError(f"Kalshi get_positions failed HTTP {resp.status}: {text}")

    async def get_orders(self) -> dict[str, Any]:
        """Fetch active/open orders."""
        if not self.is_authenticated():
            raise RuntimeError("Kalshi credentials not configured")

        session = await self._get_session()
        path = "/trade-api/v2/portfolio/orders"
        url = f"{self.api_url}/portfolio/orders"
        headers = self._create_auth_headers("GET", path)

        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            text = await resp.text()
            raise RuntimeError(f"Kalshi get_orders failed HTTP {resp.status}: {text}")

    async def create_order(self, order: OrderRequest) -> OrderExecutionResult:
        """Submit an order to Kalshi."""
        if not self.is_authenticated():
            return OrderExecutionResult(
                venue=Venue.KALSHI,
                client_order_id=order.client_order_id,
                opportunity_id=order.opportunity_id,
                status=ExecutionStatus.FAILED,
                price=order.price,
                error_message="Kalshi credentials not configured",
            )

        session = await self._get_session()
        path = "/trade-api/v2/portfolio/orders"
        url = f"{self.api_url}/portfolio/orders"
        headers = self._create_auth_headers("POST", path)

        # On Kalshi, price in cents (1 to 99)
        price_cents = int((order.price * Decimal("100")).quantize(Decimal("1")))
        price_cents = max(1, min(99, price_cents))

        payload: dict[str, Any] = {
            "ticker": order.ticker,
            "client_order_id": order.client_order_id,
            "action": "buy" if order.side.value == "BUY" else "sell",
            "side": "yes" if order.outcome.value == "YES" else "no",
            "type": "limit",
            "count": order.quantity,
            "expiration_ts": int(time.time()) + 5,
        }

        if order.outcome.value == "YES":
            payload["yes_price"] = price_cents
        else:
            payload["no_price"] = price_cents

        submit_ns = time.perf_counter_ns()
        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                ack_ns = time.perf_counter_ns()
                data = await resp.json()

                if resp.status in (200, 201):
                    order_info = data.get("order", data)
                    order_id = order_info.get("order_id") or order_info.get("id")
                    status_raw = order_info.get("status", "executed")
                    is_filled = status_raw in ("executed", "filled")
                    status = ExecutionStatus.FILLED if is_filled else ExecutionStatus.ACKNOWLEDGED

                    fill_price = order.price
                    if "yes_price" in order_info and order.outcome.value == "YES":
                        fill_price = Decimal(str(order_info["yes_price"])) / Decimal("100")
                    elif "no_price" in order_info and order.outcome.value == "NO":
                        fill_price = Decimal(str(order_info["no_price"])) / Decimal("100")

                    fee_paid = Decimal("0.00")
                    if "fee" in order_info:
                        fee_paid = Decimal(str(order_info["fee"])) / Decimal("100")

                    return OrderExecutionResult(
                        venue=Venue.KALSHI,
                        order_id=order_id,
                        client_order_id=order.client_order_id,
                        opportunity_id=order.opportunity_id,
                        status=status,
                        price=order.price,
                        fill_price=fill_price,
                        quantity=order.quantity,
                        fill_quantity=order.quantity if is_filled else 0,
                        fee_paid=fee_paid,
                        submitted_at_ns=submit_ns,
                        ack_at_ns=ack_ns,
                        filled_at_ns=ack_ns if is_filled else 0,
                        raw_response=data,
                    )
                else:
                    return OrderExecutionResult(
                        venue=Venue.KALSHI,
                        client_order_id=order.client_order_id,
                        opportunity_id=order.opportunity_id,
                        status=ExecutionStatus.REJECTED,
                        price=order.price,
                        submitted_at_ns=submit_ns,
                        ack_at_ns=ack_ns,
                        error_message=f"HTTP {resp.status}: {data}",
                        raw_response=data,
                    )
        except Exception as e:
            return OrderExecutionResult(
                venue=Venue.KALSHI,
                client_order_id=order.client_order_id,
                opportunity_id=order.opportunity_id,
                status=ExecutionStatus.FAILED,
                price=order.price,
                submitted_at_ns=submit_ns,
                error_message=str(e),
            )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order on Kalshi."""
        if not self.is_authenticated():
            return False

        session = await self._get_session()
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        url = f"{self.api_url}/portfolio/orders/{order_id}"
        headers = self._create_auth_headers("DELETE", path)

        async with session.delete(url, headers=headers) as resp:
            return resp.status in (200, 204)
