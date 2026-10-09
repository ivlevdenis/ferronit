# Models & query DSL — `ferrox.contrib.rawmodel`

Typed row models over the raw asyncpg layer. A model is an immutable
`msgspec.Struct` whose fields are the table columns in `SELECT *` order, so
row → instance is a positional `cls(*row)` — no dict, no reflection, C-level
JSON for free.

```python
from ferrox.contrib.rawmodel import Model, select

class User(Model):
    __table__ = "users"        # optional; defaults to pluralised class name
    id: int = 0
    name: str = ""
    age: int = 0
```

The table name defaults to the lowercased, pluralised class name (`User` →
`users`, `Category` → `categories`, `Person` → `people`).

## Queries

```python
q = select(User).where(User.c.age > 18).order_by(User.c.name).limit(50)
sql, params = q.compile()
# ("SELECT id, name, age FROM users WHERE (age > $1) ORDER BY name ASC LIMIT $2", [18, 50])
```

| Method | Meaning |
|---|---|
| `where(*conds)` | add `AND`-combined conditions |
| `order_by(*specs)` | columns (`.asc()`/`.desc()`) or raw terms |
| `limit(n)` / `offset(n)` | pagination |
| `first()` / `one()` | `LIMIT 1` |
| `columns(*names)` | select specific columns |
| `count()` | `SELECT count(*)` (use with `fetch_value`) |
| `aggregate(expr)` | `SELECT sum(col)` etc. (use with `fetch_value`) |
| `compile()` | → `(sql, params)` with continuous `$N` |

## Conditions

Three equivalent styles — mix freely in one `where`:

### 1. Column DSL

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

### 2. Operators `|` `&` `~`

```python
select(User).where((User.c.age < 18) | (User.c.age > 65))       # OR
select(User).where((User.c.age > 0) & User.c.email.is_null())   # AND
select(User).where(~User.c.id.in_([1, 2]))                      # NOT
```

### 3. WHERE mini-DSL (string)

```python
select(User).where("age > 18 and (name ilike '%a%' or id in (1, 2, 3))")
select(User).where("email is not null and id not in (1, 2) and age between 18 and 65")
```

The parser supports `and/or/not`, parentheses, `= != < <= > >=`, `in (...)` /
`not in (...)`, `like` / `ilike`, `is [not] null`, `between ... and ...`.
Column names are **validated against the model** (typo → `ValueError`) and every
value is bound as a parameter — no SQL injection is possible. `= null` / `!= null`
map to `IS [NOT] NULL`.

## Repositories

```python
from ferrox.contrib.rawmodel import RawModelRepository

repo = RawModelRepository(conn, User)              # or uow.model(User)

user  = await repo.get(1)                          # → User | None (identity-map cached)
users = await repo.list(User.c.age > 18, limit=50)
users = await repo.fetch(select(User).where(User.c.age > 18))
one   = await repo.fetch_one(select(User).order_by(User.c.id.desc()))
n     = await repo.fetch_value(select(User).count())
saved = await repo.save(User(name="Ada"))          # server id via RETURNING
fresh = await repo.update(1, {"name": "Grace"})
await repo.delete(1)
```

## Unit of work & identity map

```python
async with RawUnitOfWork(pool, readonly=True) as uow:
    repo = uow.model(User)            # shares the unit's connection + identity map
    a = await repo.get(1)
    b = await repo.get(1)
    assert a is b                     # same instance — one round-trip
```

`get()` populates the identity map; `list()`/`fetch()` deliberately do not
(avoiding ~50 µs per 1000 rows). The map lives for one unit of work.
