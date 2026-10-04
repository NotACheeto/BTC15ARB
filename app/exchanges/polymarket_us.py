"""High-performance asynchronous client for Polymarket US (polymarket.us).

Supports direct HTTP connection pooling, Ed25519 request signing,
safe preflight previewing, positions, orders, and emergency close-position.
"""

from decimal import Decimal
import time
from typing import Any
import aiohttp
from polymarket_us.auth import create_auth_headers

from app.config import Settings
from app.latency import latency_tracker
from app.models import ExecutionStatus, OrderExecutionResult, OrderRequest, Venue


class PolymarketUSClient:
    """Async Polymarket US trading and market data client."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None):
        self.settings = settings
        self.key_id = settings.POLYMARKET_US_KEY_ID
        self.secret_key = settings.POLYMARKET_US_SECRET_KEY or settings.POLYMARKET_US_SECRET
        self.gateway_base_url = settings.POLYMARKET_US_GATEWAY_URL.rstrip("/")
        self.api_base_url = settings.POLYMARKET_US_API_URL.rstrip("/")

        self._session = session
        self._owns_session = False

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

    def is_authenticated(self) -> bool:
        return bool(self.key_id and self.secret_key)

    def _get_headers(self, method: str, path: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.key_id and self.secret_key:
            auth_headers = create_auth_headers(self.key_id, self.secret_key, method, path)
            headers.update(auth_headers)
        return headers

    async def get_bbo(self, market_slug: str) -> dict[str, Any]:
        """Fetch Best Bid & Offer from Polymarket US API."""
        session = await self._get_session()
        path = f"/v1/markets/{market_slug}/bbo"
        url = f"{self.api_base_url}{path}"
        headers = self._get_headers("GET", path) if self.is_authenticated() else {}

        start_ns = time.perf_counter_ns()
        try:
            async with session.get(url, headers=headers) as resp:
                elapsed_ns = time.perf_counter_ns() - start_ns
                latency_tracker.polymarket_rest_rtt.record_ns(elapsed_ns)
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                raise RuntimeError(f"Polymarket get_bbo failed HTTP {resp.status}: {text}")
        except Exception as e:
            elapsed_ns = time.perf_counter_ns() - start_ns
            latency_tracker.polymarket_rest_rtt.record_ns(elapsed_ns)
            raise e

    async def get_account_balances(self) -> dict[str, Any]:
        """Fetch cash and portfolio balances."""
        if not self.is_authenticated():
            raise RuntimeError("Polymarket credentials not configured")

        session = await self._get_session()
        path = "/v1/account/balances"
        url = f"{self.api_base_url}{path}"
        headers = self._get_headers("GET", path)

        start_ns = time.perf_counter_ns()
        try:
            async with session.get(url, headers=headers) as resp:
                elapsed_ns = time.perf_counter_ns() - start_ns
                latency_tracker.polymarket_rest_rtt.record_ns(elapsed_ns)
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                raise RuntimeError(f"Polymarket get_balances failed HTTP {resp.status}: {text}")
        except Exception as e:
            elapsed_ns = time.perf_counter_ns() - start_ns
            latency_tracker.polymarket_rest_rtt.record_ns(elapsed_ns)
            raise e

    async def get_positions(self) -> dict[str, Any]:
        """Fetch user positions."""
        if not self.is_authenticated():
            raise RuntimeError("Polymarket credentials not configured")

        session = await self._get_session()
        path = "/v1/portfolio/positions"
        url = f"{self.api_base_url}{path}"
        headers = self._get_headers("GET", path)

        start_ns = time.perf_counter_ns()
        try:
            async with session.get(url, headers=headers) as resp:
                elapsed_ns = time.perf_counter_ns() - start_ns
                latency_tracker.polymarket_rest_rtt.record_ns(elapsed_ns)
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                raise RuntimeError(f"Polymarket get_positions failed HTTP {resp.status}: {text}")
        except Exception as e:
            elapsed_ns = time.perf_counter_ns() - start_ns
            latency_tracker.polymarket_rest_rtt.record_ns(elapsed_ns)
            raise e

    async def get_open_orders(self) -> dict[str, Any]:
        """List open orders."""
        if not self.is_authenticated():
            raise RuntimeError("Polymarket credentials not configured")

        session = await self._get_session()
        path = "/v1/orders/open"
        url = f"{self.api_base_url}{path}"
        headers = self._get_headers("GET", path)

        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            text = await resp.text()
            raise RuntimeError(f"Polymarket get_open_orders failed HTTP {resp.status}: {text}")

    async def preview_order(self, order: OrderRequest) -> dict[str, Any]:
        """Preview an order without submitting it (non-destructive preflight check)."""
        if not self.is_authenticated():
            raise RuntimeError("Polymarket credentials not configured")

        session = await self._get_session()
        path = "/v1/order/preview"
        url = f"{self.api_base_url}{path}"
        headers = self._get_headers("POST", path)

        intent = "ORDER_INTENT_BUY_LONG" if order.outcome.value == "YES" else "ORDER_INTENT_BUY_SHORT"
        payload = {
            "request": {
                "marketSlug": order.market_id,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{order.price:.4f}", "currency": "USD"},
                "quantity": order.quantity,
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                "synchronousExecution": True,
            }
        }

        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            if resp.status in (200, 201):
                return data
            raise RuntimeError(f"Polymarket preview_order failed HTTP {resp.status}: {data}")

    async def create_order(self, order: OrderRequest) -> OrderExecutionResult:
        """Submit live order to Polymarket US."""
        if not self.is_authenticated():
            return OrderExecutionResult(
                venue=Venue.POLYMARKET_US,
                client_order_id=order.client_order_id,
                opportunity_id=order.opportunity_id,
                status=ExecutionStatus.FAILED,
                price=order.price,
                error_message="Polymarket credentials not configured",
            )

        session = await self._get_session()
        path = "/v1/orders"
        url = f"{self.api_base_url}{path}"
        headers = self._get_headers("POST", path)

        market_slug = order.ticker if order.ticker else order.market_id
        if order.side.value == "SELL":
            intent = "ORDER_INTENT_SELL_LONG" if order.outcome.value == "YES" else "ORDER_INTENT_SELL_SHORT"
        else:
            intent = "ORDER_INTENT_BUY_LONG" if order.outcome.value == "YES" else "ORDER_INTENT_BUY_SHORT"

        payload = {
            "marketSlug": market_slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{order.price:.4f}", "currency": "USD"},
            "quantity": order.quantity,
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
        }

        submit_ns = time.perf_counter_ns()
        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                ack_ns = time.perf_counter_ns()
                data = await resp.json()

                if resp.status in (200, 201):
                    order_obj = data.get("order") if isinstance(data.get("order"), dict) else data
                    exec_obj = data.get("execution") if isinstance(data.get("execution"), dict) else {}

                    order_id = (
                        data.get("orderId")
                        or order_obj.get("id")
                        or order_obj.get("orderId")
                        or data.get("id")
                        or ""
                    )

                    status_raw = str(
                        order_obj.get("status")
                        or data.get("status")
                        or ""
                    ).upper()

                    # Extract filled quantity safely
                    executed_qty = 0
                    for q_key in ("filledQuantity", "executedQuantity", "cumQuantity", "fillQuantity", "executedShares"):
                        val = exec_obj.get(q_key) or order_obj.get(q_key) or data.get(q_key)
                        if val is not None:
                            try:
                                executed_qty = int(Decimal(str(val)))
                                break
                            except Exception:
                                pass

                    if status_raw in ("ORDER_STATUS_FILLED", "FILLED", "MATCHED", "EXECUTED"):
                        status = ExecutionStatus.FILLED
                        fill_qty = executed_qty if executed_qty > 0 else order.quantity
                    elif status_raw in ("ORDER_STATUS_PARTIALLY_FILLED", "PARTIALLY_FILLED") or (0 < executed_qty < order.quantity):
                        status = ExecutionStatus.PARTIALLY_FILLED
                        fill_qty = executed_qty
                    elif status_raw in ("ORDER_STATUS_CANCELED", "CANCELED", "CANCELLED", "ORDER_STATUS_EXPIRED", "EXPIRED", "ORDER_STATUS_REJECTED", "REJECTED"):
                        status = ExecutionStatus.REJECTED
                        fill_qty = 0
                    elif status_raw in ("ORDER_STATUS_OPEN", "OPEN", "PENDING", "ORDER_STATUS_NEW"):
                        status = ExecutionStatus.ACKNOWLEDGED
                        fill_qty = executed_qty
                    else:
                        # Fail-closed: Never assume filled unless verified
                        status = ExecutionStatus.ACKNOWLEDGED
                        fill_qty = executed_qty

                    # Extract fill price safely
                    fill_price = order.price
                    for p_key in ("fillPrice", "executionPrice", "avgPrice", "averagePrice"):
                        val = exec_obj.get(p_key) or order_obj.get(p_key) or data.get(p_key)
                        if val is not None:
                            if isinstance(val, dict) and "value" in val:
                                fill_price = Decimal(str(val["value"]))
                                break
                            else:
                                fill_price = Decimal(str(val))
                                break

                    # Extract fees safely
                    fee_paid = Decimal("0.00")
                    for f_key in ("fee", "feePaid", "fees", "feeAmount"):
                        val = exec_obj.get(f_key) or order_obj.get(f_key) or data.get(f_key)
                        if val is not None:
                            if isinstance(val, dict) and "value" in val:
                                fee_paid = Decimal(str(val["value"]))
                                break
                            else:
                                try:
                                    fee_paid = Decimal(str(val))
                                    break
                                except Exception:
                                    pass

                    return OrderExecutionResult(
                        venue=Venue.POLYMARKET_US,
                        order_id=order_id,
                        client_order_id=order.client_order_id,
                        opportunity_id=order.opportunity_id,
                        status=status,
                        price=order.price,
                        fill_price=fill_price,
                        quantity=order.quantity,
                        fill_quantity=fill_qty,
                        fee_paid=fee_paid,
                        submitted_at_ns=submit_ns,
                        ack_at_ns=ack_ns,
                        filled_at_ns=ack_ns if fill_qty > 0 else 0,
                        raw_response=data,
                    )
                else:
                    return OrderExecutionResult(
                        venue=Venue.POLYMARKET_US,
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
                venue=Venue.POLYMARKET_US,
                client_order_id=order.client_order_id,
                opportunity_id=order.opportunity_id,
                status=ExecutionStatus.FAILED,
                price=order.price,
                submitted_at_ns=submit_ns,
                error_message=str(e),
            )

    async def cancel_order(self, order_id: str, market_slug: str) -> bool:
        """Cancel a resting order."""
        if not self.is_authenticated():
            return False

        session = await self._get_session()
        path = f"/v1/order/{order_id}/cancel"
        url = f"{self.api_base_url}{path}"
        headers = self._get_headers("POST", path)
        payload = {"marketSlug": market_slug}

        async with session.post(url, headers=headers, json=payload) as resp:
            return resp.status in (200, 204)

    async def close_position(self, market_slug: str) -> dict[str, Any]:
        """Emergency unwind of an unhedged position using native close-position endpoint."""
        if not self.is_authenticated():
            raise RuntimeError("Polymarket credentials not configured")

        session = await self._get_session()
        path = "/v1/order/close-position"
        url = f"{self.api_base_url}{path}"
        headers = self._get_headers("POST", path)
        payload = {
            "marketSlug": market_slug,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
        }

        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            if resp.status in (200, 201):
                return data
            raise RuntimeError(f"Polymarket close_position failed HTTP {resp.status}: {data}")
