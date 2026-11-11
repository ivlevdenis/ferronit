"""HTTP adapter for the incoming ``OrderApiPort`` — turns HTTP into port calls.

Thin entry: it works only with application-layer DTOs (``CreateOrderDTO`` →
``OrderDTO``) and the port. No domain and no mapping here — ``OrderService`` in
``examples/app.py`` translates domain ↔ DTO. Mounted from the composition root
``examples/main.py`` via ``app.mount("/", HttpOrdersAdapter())``.

Endpoints:
  POST /orders              — create an order (PlaceOrder command)
  GET  /orders              — list orders (ListUserOrders query)
  GET  /orders/{id}         — an order with all its items (GetOrder query)
  POST /orders/{id}/cancel  — cancel an order (CancelOrder command)
  GET  /openapi.json
  GET  /ping
"""

from typing import Annotated

from ferronit import Body, Ferronit
from examples.app import CreateOrderDTO, OrderApiPort, OrderDTO


class HttpOrdersAdapter:
    """Incoming HTTP adapter: its routes depend only on ``OrderApiPort``.

    Registered via ``app.mount(prefix, adapter)`` — Ferronit calls :meth:`mount`
    with a ready application, so the adapter keeps no ``app`` in its constructor
    and needs no separate ``.bind()``.
    """

    def mount(self, app: Ferronit, prefix: str = "") -> None:
        """Register the routes on ``app`` under ``prefix``."""
        base = prefix.rstrip("/")

        @app.route(f"{base}/orders", methods=["POST"], summary="Create order")
        async def create_order(body: Annotated[CreateOrderDTO, Body()], port: OrderApiPort):
            order = await port.create(body)
            if order is None:
                return {"error": "items required"}, 400
            return order

        @app.route(f"{base}/orders", summary="List orders")
        async def list_orders(port: OrderApiPort) -> list[OrderDTO]:
            return await port.list_user_orders("user-1")

        @app.route(f"{base}/orders/{{order_id}}", summary="Get order")
        async def get_order(order_id: str, port: OrderApiPort):
            order = await port.get(order_id)
            if order is None:
                return {"error": "not found"}, 404
            return order

        @app.route(f"{base}/orders/{{order_id}}/cancel", methods=["POST"], summary="Cancel order")
        async def cancel_order(order_id: str, port: OrderApiPort):
            order = await port.cancel(order_id)
            if order is None:
                return {"error": "not found"}, 404
            return order

        @app.route(f"{base}/openapi.json")
        async def openapi_spec():
            return app.openapi.build()

        @app.route(f"{base}/ping")
        async def ping():
            return {"ok": True}
