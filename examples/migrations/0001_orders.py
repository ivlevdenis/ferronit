"""0001 — заказы и позиции заказа.

Пример файла миграции: так его будет создавать `ferronit migrate new`.
Схема описана шагами — либо DDL-узлами генератора, либо `sql.Raw`,
когда движок требует своего синтаксиса.

Правила:
* ``UP`` применяется по порядку, один шаг = один оператор;
* ``DOWN`` — откат, применяется в обратном порядке;
* порядок применения задаёт имя файла (``0001_``, ``0002_``, …);
* в файле нет ничего, что знает о конкретной СУБД: это работа диалекта.
"""

from ferronit.contrib import sql

UP = [
    sql.CreateTable(
        "orders",
        [
            sql.Column("id", sql.INT, pk=True),
            sql.Column("user_id", sql.INT, nullable=False),
            sql.Column("total", sql.NUMERIC, nullable=False, precision=12, scale=2),
            sql.Column("status", sql.TEXT, nullable=False, default="new"),
            # now() есть у PG и MySQL, но не у SQLite — CURRENT_TIMESTAMP есть у всех.
            sql.Column("placed_at", sql.TIMESTAMP, nullable=False, default=sql.Raw("CURRENT_TIMESTAMP")),
        ],
    ),
    sql.CreateIndex("orders_user_id_idx", "orders", ["user_id"]),
    sql.CreateTable(
        "order_items",
        [
            sql.Column("id", sql.INT, pk=True),
            sql.Column("order_id", sql.INT, nullable=False),
            sql.Column("sku", sql.TEXT, nullable=False),
            sql.Column("qty", sql.INT, nullable=False, default=1),
        ],
    ),
    sql.CreateIndex("order_items_order_id_idx", "order_items", ["order_id"]),
]

DOWN = [
    sql.DropTable("order_items"),
    sql.DropTable("orders"),
]
