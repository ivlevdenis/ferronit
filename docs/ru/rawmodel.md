# Модели и query DSL — `ferrox.contrib.rawmodel`

Типизированные модели строк поверх сырого asyncpg-слоя. Модель — неизменяемый
`msgspec.Struct`, чьи поля — колонки таблицы в порядке `SELECT *`, поэтому
строка → экземпляр это позиционный `cls(*row)` — без dict, без рефлексии, с
C-уровневым JSON «из коробки».

```python
from ferrox.contrib.rawmodel import Model, select

class User(Model):
    __table__ = "users"        # опционально; по умолчанию — плюрализованное имя класса
    id: int = 0
    name: str = ""
    age: int = 0
```

Имя таблицы по умолчанию — имя класса в нижнем регистре во множественном числе
(`User` → `users`, `Category` → `categories`, `Person` → `people`).

## Запросы

```python
q = select(User).where(User.c.age > 18).order_by(User.c.name).limit(50)
sql, params = q.compile()
# ("SELECT id, name, age FROM users WHERE (age > $1) ORDER BY name ASC LIMIT $2", [18, 50])
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

## Условия

Три равноправных стиля — смешивайте свободно в одном `where`:

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

### 2. Операторы `|` `&` `~`

```python
select(User).where((User.c.age < 18) | (User.c.age > 65))       # OR
select(User).where((User.c.age > 0) & User.c.email.is_null())   # AND
select(User).where(~User.c.id.in_([1, 2]))                      # NOT
```

### 3. Мини-DSL строкой

```python
select(User).where("age > 18 and (name ilike '%a%' or id in (1, 2, 3))")
select(User).where("email is not null and id not in (1, 2) and age between 18 and 65")
```

Парсер поддерживает `and/or/not`, скобки, `= != < <= > >=`, `in (...)` /
`not in (...)`, `like` / `ilike`, `is [not] null`, `between ... and ...`.
Имена колонок **сверяются с моделью** (опечатка → `ValueError`), каждое значение
уходит bound-параметром — SQL-инъекция невозможна. `= null` / `!= null`
превращаются в `IS [NOT] NULL`.

## Репозитории

```python
from ferrox.contrib.rawmodel import RawModelRepository

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

## Unit of work и identity map

```python
async with RawUnitOfWork(pool, readonly=True) as uow:
    repo = uow.model(User)            # разделяет соединение и identity map юнита
    a = await repo.get(1)
    b = await repo.get(1)
    assert a is b                     # тот же экземпляр — один round-trip
```

`get()` наполняет identity map; `list()`/`fetch()` намеренно нет (избегаем
~50 µs на 1000 строк). Карта живёт в рамках одного unit of work.
