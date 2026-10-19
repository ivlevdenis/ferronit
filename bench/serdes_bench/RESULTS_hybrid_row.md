# Промежуточный класс между `msgspec.Struct` и строкой драйвера (16.10.2026)

Вопрос: можно ли сделать класс, который одновременно и строка драйвера (asyncpg
`Record`), и `msgspec.Struct` — чтобы драйвер сам заполнял модель и не было отдельного
шага маппинга.

**Ответ: нельзя — конструкторы взаимоисключающие на обеих сторонах.** Проверено тремя
опытами (`bench/exp_composite_row.py`, `bench/exp_hybrid_row.py`).

## 1. Наследование `Record` + `Struct` невозможно

```
class Hybrid(asyncpg.Record, msgspec.Struct): id: int = 0
  -> TypeError: Struct base classes cannot define __new__
class Hybrid2(msgspec.Struct, asyncpg.Record): id: int = 0
  -> TypeError: Struct base classes cannot define __new__
```

`asyncpg.Record` — C-тип со своим `__new__`, а msgspec запрещает структурным базовым
классам его определять (у него фиксированная раскладка полей в памяти). Порядок базовых
классов значения не имеет.

## 2. `record_class` у asyncpg не может заполнять Struct

```
class RecMethods(asyncpg.Record):        # только методы — разрешено
    __slots__ = ()
    def as_dict(self): return dict(self)
conn.fetch("SELECT id, name, email …", record_class=RecMethods)   # работает

class RecInit(asyncpg.Record):           # свой конструктор — запрещено
    def __init__(self, *a): pass
  -> InterfaceError: record_class must not redefine __new__ or __init__
```

Проверка в `asyncpg/connection.py::_check_record_class`. То есть драйвер готов создавать
наш подкласс, но заполнять поля `Struct` он не имеет права — а без конструктора Struct'у
поля не задать (msgspec не даёт ни `__new__`, ни прямую запись слотов).

## 3. msgspec не кодирует произвольные классы

```
класс со слотами + __struct_fields__  -> TypeError: Encoding objects of type SlotStruct is unsupported
вьюха над Record с __struct_fields__  -> TypeError: Encoding objects of type SlotView is unsupported
Record                                -> TypeError: Encoding objects of type …Record is unsupported
list(Record)                          -> b'[1,"user_1","u1@example.com"]'   (позиции, без имён полей)
dict(Record)                          -> b'{"id":1,…}'                        (медленнее модели: ×2,2)
```

Значит и «вьюха» над `Record` (объект без копирования значений, который msgspec кодирует
сам) не проходит: msgspec принимает только `Struct`, dataclass, namedtuple и контейнеры.

## Что стоило бы двойное заполнение (шаг маппинга), и замена Struct

`Record → модель` — это единственное, что дал бы «слитый» класс: на 3 колонках
**7,8 µs на 100 строк и 74 µs на 1000** (4% и 15% запроса). Декодирование полей самим
драйвером (0,37 µs/строка) остаётся при любом раскладе.

Замены `Struct` на другой кодируемый тип — только хуже (µs на строку, 3 поля, 1000 строк):

| тип | сборка | msgspec JSON | форма JSON (1000 строк) |
|---|---|---|---|
| **msgspec.Struct** | **0,06** | **0,03** | `[{"id":1,"name"…` — 39 680 б |
| dataclass | 0,09 | 0,06 | та же объектом — 39 680 б |
| namedtuple | 0,13 | 0,02 | `[[1,"a1","b1"]…` — 19 680 б (массивы) |
| класс со слотами | 0,09 | — | не кодируется (`TypeError`) |
| tuple | 0,03 | 0,02 | `[[1,"a1","b1"]…` — массивы, имён нет |

## Композитный кодек asyncpg — тоже не выход

Единственная форма, которую asyncpg принимает для композитных типов, — tuple-кодек;
но значение всё равно приходит `Record`'ом, и путь дороже:

| операция | limit=100 | limit=1000 |
|---|---|---|
| `fetch` обычных колонок | 56,0 µs | 268,9 µs |
| `fetch` композита (`ROW(…)::exp_row`) | 87,8 µs | 412,3 µs |
| `Record → модель` | 7,8 µs | 74,1 µs |
| композит → модель | 9,4 µs | 93,0 µs |

## Что вместо этого работает (уже измерено)

1. **JSON собирает PostgreSQL** (`string_agg(row_to_json(t)::text, ',')`): ×1,72 на 100
   строках, ×3,14 на 1000 — Python не трогает ни строки, ни поля.
2. **Драйвер по проводу PostgreSQL в Rust** (спайк `ferrox-db`): ×2,4–2,5 — тоже без
   Python-объектов на строку.
   Оба варианта убирают не шаг маппинга (7,8/74 µs), а весь путь данных через Python.
