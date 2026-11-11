"""0001 — orders and order items.

An example migration file: this is how `ferronit migrate new` will create it.
The schema is described as steps — either generator DDL nodes or `sql.Raw`
when the engine needs its own syntax.

Rules:
* ``UP`` is applied in order, one step = one statement;
* ``DOWN`` is the rollback, applied in reverse order;
* the file name sets the application order (``0001_``, ``0002_``, …);
* nothing in the file knows about a specific DBMS: that is the dialect's job.
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
            # now() exists in PG and MySQL but not in SQLite — CURRENT_TIMESTAMP exists everywhere.
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
