"""HTTP-адаптер входящего порта ``OrderApiPort`` — переводит HTTP в вызовы порта.

Тонкий вход: работает только с DTO прикладного слоя (``CreateOrderDTO`` →
``OrderDTO``) и портом. Домена и маппинга здесь нет — перевод домен ↔ DTO делает
``OrderService`` в ``examples/app.py``. Монтируется из точки сборки
``examples/main.py`` через ``app.mount("/", HttpOrdersAdapter())``.

Endpoints:
  POST /orders              — создать заказ (команда PlaceOrder)
  GET  /orders              — список заказов (запрос ListUserOrders)
  GET  /orders/{id}         — заказ со всеми позициями (запрос GetOrder)
  POST /orders/{id}/cancel  — отменить заказ (команда CancelOrder)
  GET  /openapi.json
  GET  /ping
"""

from typing import Annotated

from ferronit import Body, Ferronit
from examples.app import CreateOrderDTO, OrderApiPort, OrderDTO


class HttpOrdersAdapter:
    """Входящий HTTP-адаптер: маршруты зависят только от ``OrderApiPort``.

    Регистрируется через ``app.mount(prefix, adapter)`` — Ferronit вызывает
    :meth:`mount` с готовым приложением, поэтому адаптер не держит ``app`` в
    конструкторе и не требует отдельного ``.bind()``.
    """

    def mount(self, app: Ferronit, prefix: str = "") -> None:
        """Зарегистрировать маршруты на ``app`` под ``prefix``."""
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
        def openapi_spec():
            return app.openapi.build()

        @app.route(f"{base}/ping")
        async def ping():
            return {"ok": True}
