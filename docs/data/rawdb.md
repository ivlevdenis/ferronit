# Сырой asyncpg + модели — `rawdb` и `rawmodel`

## Зачем

Самый быстрый способ ходить в PostgreSQL из Python — без ORM. `rawdb` — тонкий,
явный слой на asyncpg (dict-строки, фильтры — SQL-фрагменты с bound-параметрами).
Поверх него `rawmodel` даёт типизированные модели строк и query DSL, почти не
теряя в скорости.

## `rawdb` — сырой слой

```python
from ferronit.contrib.rawdb import Pool, RawUnitOfWork, Condition, create_pool

pool = await create_pool("postgresql://user:***@host/db", min_size=1, max_size=16)

async with pool.uow() as uow:                     # BEGIN / COMMIT (или rollback)
    repo = uow["users"]                           # имя таблицы → RawRepository
    row = await repo.save({"name": "Ada"})        # → {"name": "Ada", "id": 1}
    await repo.update(1, {"name": "Grace"})
    users = await repo.list(Condition("age > $1", [18]), limit=50)
    await repo.delete(1)
```

- `Pool` — обёртка над asyncpg-пулом; `pool.uow(readonly=False)` отдаёт
  `RawUnitOfWork` (одно соединение, одна транзакция), `pool.close()` закрывает.
- `RawUnitOfWork(pool, readonly=False)` — то же напрямую.
  `readonly=True` пропускает `BEGIN` (экономия одного round-trip) для read-only.
- `Condition(sql, params)` — фрагмент с PostgreSQL-плейсхолдерами (`$1`…).
  Комбинируется операторами:

```python
c = Condition("age > $1", [18]) & Condition("name ILIKE $1", ["%a%"])
c = Condition("status = $1", ["a"]) | Condition("status = $1", ["b"])
c = ~Condition("email IS NULL")          # NOT (...)
```

Плейсхолдеры перенумеровываются автоматически при комбинировании.

Жизненный цикл: создавайте пул **внутри** event loop сервера — обычно в
`@app.lifespan` (setup до `yield`, `pool.close()` после); используйте **один**
`RawUnitOfWork` на запрос. Несколько пулов одного типа различаются
`Annotated[Pool, "marker"]` — см. [архитектуру](../architecture.md).

## `rawmodel` — типизированные модели и query DSL

Модель — неизменяемый `msgspec.Struct`, чьи поля — колонки таблицы в порядке
`SELECT *`. Строка → экземпляр это позиционный `cls(*row)` — без dict, без
рефлексии, с C-уровневым JSON «из коробки».

```python
from ferronit.contrib.rawmodel import Model, select

class User(Model):
    __table__ = "users"        # опционально; по умолчанию — плюрализованное имя класса
    id: int = 0
    name: str = ""
    age: int = 0
```

Имя таблицы по умолчанию — имя класса в нижнем регистре во множественном числе
(`User` → `users`, `Category` → `categories`, `Person` → `people`).

### Запросы

```python
q = select(User).where(User.c.age > 18).order_by(User.c.name).limit(50)
sql, params = q.compile()
# ("SELECT id, name, age FROM users WHERE (age > $1) ORDER BY name LIMIT $2", [18, 50])
```

| Метод | Значение |
|---|---|
| `where(*conds)` | добавить условия через `AND` |
| `order_by(*specs)` | колонки (`.asc()`/`.desc()`) или сырые термы |
| `limit(n)` / `offset(n)` | пагинация |
| `first()` / `one()` | `LIMIT 1` |
| `columns(*names)` | выбрать конкретные колонки |
| `count()` | `SELECT count(*)` (забирать через `fetch_value`) |
| `aggregate(expr)` | `SELECT sum(col)` и т.п. (через `fetch_value`) |
| `compile()` | → `(sql, params)` с непрерывными `$N` |

### Условия — три равноправных стиля

**1. Column DSL**

```python
User.c.age > 18            # > >= < <=
User.c.name == "Ada"       # == / !=
User.c.email == None       # → IS NULL
User.c.id.in_([1, 2, 3])   # = ANY($1)
User.c.id.not_in([1, 2])
User.c.name.like("Jo%") / .ilike("%a%")
User.c.email.is_null() / .is_not_null()
User.c.age.between(18, 65)
```

**2. Операторы `|` `&` `~`**

```python
select(User).where((User.c.age < 18) | (User.c.age > 65))       # OR
select(User).where((User.c.age > 0) & User.c.email.is_null())   # AND
select(User).where(~User.c.id.in_([1, 2]))                      # NOT
```

**3. Мини-DSL строкой**

```python
select(User).where("age > 18 and (name ilike '%a%' or id in (1, 2, 3))")
```

Парсер поддерживает `and/or/not`, скобки, `= != < <= > >=`, `in (...)` /
`not in (...)`, `like` / `ilike`, `is [not] null`, `between ... and ...`.
Имена колонок **сверяются с моделью** (опечатка → `ValueError`), каждое
значение уходит bound-параметром — SQL-инъекция невозможна. `= null` / `!= null`
превращаются в `IS [NOT] NULL`.

### Репозитории

```python
from ferronit.contrib.rawmodel import RawModelRepository

repo = RawModelRepository(conn, User)              # или uow.model(User)

user  = await repo.get(1)                          # → User | None (кэш identity map)
users = await repo.list(User.c.age > 18, limit=50)
users = await repo.fetch(select(User).where(User.c.age > 18))
one   = await repo.fetch_one(select(User).order_by(User.c.id.desc()))
n     = await repo.fetch_value(select(User).count())
saved = await repo.save(User(name="Ada"))          # server id через RETURNING
fresh = await repo.update(1, {"name": "Grace"})
await repo.delete(1)
```

SQL-строки репозитория (`INSERT`/`SELECT`/`DELETE` и `list` без фильтров) собираются
один раз на `(модель, таблица, pk)` и кэшируются (`lru_cache`) — на каждый запрос
остаётся только выполнение, без пересборки строк.

Дополнительные операции: `upsert(obj)` (вставить или перезаписать строку по
первичному ключу — `ON CONFLICT ... DO UPDATE`), `save_many(objs)` (bulk-insert
одним оператором) и `delete_by(column, value)` (удалить строки по произвольной
колонке — её использует агрегатный репозиторий для синхронизации дочерних таблиц).

### Unit of work и identity map

```python
async with RawUnitOfWork(pool, readonly=True) as uow:
    repo = uow.model(User)            # разделяет соединение и identity map юнита
    a = await repo.get(1)
    b = await repo.get(1)
    assert a is b                     # тот же экземпляр — один round-trip
```

`get()` наполняет identity map; `list()`/`fetch()` намеренно нет (избегаем
лишних ~50 µs на 1000 строк). Карта живёт в рамках одного unit of work.

### Связи

Связь объявляется полем с `Annotated[..., Relation(fk)]` — и грузится **всегда**
при чтении модели (без lazy/eager: раз поле есть, оно нужно). Стратегия загрузки
зависит от объёма: точечное чтение (`get`/`fetch_one`) подтягивает связь одним
`LEFT JOIN` (родительский SELECT уходит в подзапрос `t0` — один round-trip без
fan-out), массовое (`list`/`fetch`) — отдельным `SELECT … WHERE fk = ANY($1)`,
который не дублирует родительские колонки и на больших наборах быстрее JOIN.

```python
from typing import Annotated
from ferronit.contrib.rawmodel import Model, Relation

class OrderItem(Model):
    __table__ = "order_items"
    id: int = 0
    order_id: int = 0
    name: str = ""

class Order(Model):
    __table__ = "orders"
    id: int = 0
    user_id: int = 0
    items: Annotated[list[OrderItem], Relation("order_id")] = []   # 1:N

async with RawUnitOfWork(pool, readonly=True) as uow:
    orders = await uow.model(Order).list()
    for o in orders:
        print(o.items)   # уже подтянуты
```

- `list[T]` → один-ко-многим (поле-список), голый `T` → один-к-одному (один объект).
- `Relation("order_id", pk="id")` — FK в дочерней таблице и PK родителя (по умолчанию `id`).
- Связь сериализуется в JSON как вложенный объект/список (msgspec).

### Явный join

Помимо декларативных `Relation`, модель можно джойнить прямо в запросе:
`Query.join` добавляет `LEFT JOIN` по умолчанию, а `fetch_joined()` возвращает
кортежи `(родитель, [дети])`:

```python
q = select(Order).join(OrderItem, fk="order_id")                       # FK-джойн
q = select(Order).join(OrderItem, on=Order.c.id == OrderItem.c.order_id)  # по колонкам
q = select(Order).join(OrderItem, fk="order_id", kind=sql.INNER)       # тип джойна
rows = await repo.fetch_joined(q)                                      # list[(Order, list[OrderItem])]
```

- `fk` — имя FK в джойн-модели, `pk` — PK родителя (по умолчанию `id`).
- `on` — сравнение колонок (`Parent.c.x == Child.c.y`); обе колонки должны
  принадлежать родителю и джойн-модели соответственно.
- `kind` — `sql.LEFT` (по умолчанию), `sql.INNER`, `sql.RIGHT`, `sql.FULL`.
  Движок без такого джойна отклоняет statement громко: `FULL` на MySQL даёт
  `DialectError`, а не SQL, который сервер потом отвергнет.
- Результат — кортеж `(родитель, дети_j1, дети_j2, …)`, по одному списку на каждый
  `join`; джойны выполняются одним SQL (родительский SELECT уходит в подзапрос `t0`).

### Агрегатный репозиторий — Mapper + Repository

Чтобы сохранять доменный агрегат целиком (заголовок + дочерние таблицы) в одну
транзакцию, есть пара `Mapper` и обобщённый `Repository`:

```python
from ferronit.contrib.rawmodel import Mapper, Repository

class OrderMapper(Mapper[Order, OrderModel]):
    def to_aggregate(self, m):
        return Order(m.id, m.user_id, m.items)          # из модели строки + связей
    def from_aggregate(self, a):
        return OrderModel(id=a.id, user_id=a.user_id, items=a.items)

class OrderRepository(Repository[Order, OrderModel, OrderMapper, ShopPool]):
    async def by_user(self, user_id):
        return await self.list(OrderModel.c.user_id == user_id)
```

Generic `Repository[A, M, MAP, P]` — агрегат `A`, модель строки `M`, маппер `MAP` и
тип пула `P`. Классы `aggregate` / `model` / `mapper_cls` / `pool` извлекаются из
`__orig_bases__` автоматически (`__init_subclass__`); инстанс создаётся от пула:
`repo = OrderRepository(shop_pool)`.

Поведение:

- `get(id)` / `list(*filters)` — читают через `uow(readonly=True)` и переводят
  строки в агрегаты через `mapper.to_aggregate` (связи уже подтянуты моделью).
- `save(aggregate)` — `mapper.from_aggregate` → `upsert` заголовка, затем
  `delete_by(fk, parent_pk)` дочерних и `save_many` новых — всё в одной UoW.
- `delete(id)` — удаляет дочерние строки по FK, затем заголовок.
- `uow(*, readonly=False)` — открыть UoW для сценария из нескольких операций.

## Много клиентов: как разводятся транзакции

Клиентов разводит пул, а не приложение: на каждый запрос берётся своё соединение, в нём
открывается один UoW (`BEGIN` … `COMMIT`), после коммита соединение возвращается в пул.
Транзакции параллельных клиентов идут одновременно и не видят друг друга. Проверено замером
(`bench/_probe_tx_isolation.py`, `bench/_probe_tx_transfer.py`, 50 клиентов):

- внутри своей транзакции клиент видел **2 свои строки и 0 чужих**, после всех коммитов — 100 строк
  (2 × 50), пул вернулся целиком (свободно 50 из 50) — утечек нет;
- из 50 транзакций 5 упали в середине → в таблице 90 строк (2 × 45), данные остальных целы —
  откат не задевает соседей;
- **разделения мало: конкурирующая запись требует атомарности.** 50 клиентов × 40 инкрементов:
  чтение-правка-запись в Python — счётчик 40 из 2000 (потеряно 1960), `UPDATE v = v + 1` — 2000 из
  2000 за 2,25 с, `SELECT … FOR UPDATE` — 2000 за 3,67 с;
- перевод между счетами (2000 переводов): чтение-правка-запись дала расхождение суммы балансов
  **−56** из 200 000, атомарный `UPDATE` в одной UoW — 0 и быстрее (6 629 против 4 293 переводов/с).

Правила:

- один UoW на HTTP-запрос; чтения — `uow(readonly=True)` (без `BEGIN`);
- **UoW/сессию не делать синглтоном в DI** (это состояние одного соединения), синглтон — только
  пул; UoW создаётся на запрос;
- конкурентную арифметику отдавать базе: `UPDATE … WHERE условие RETURNING …`, `INSERT … ON CONFLICT`;
  нужен read-then-write под защитой — `SELECT … FOR UPDATE` или `SERIALIZABLE` + retry;
- не держать транзакцию через внешние вызовы (HTTP, LLM, файлы) — соединение занято;
- `max_size` пула × число воркеров ≤ `max_connections`;
- ответ клиенту — только после `COMMIT`.
