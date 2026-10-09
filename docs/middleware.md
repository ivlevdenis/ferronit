# Middleware и contrib

## Middleware

Middleware — это `(request, next_handler) -> response`, синхронный или
асинхронный. Полезно для сквозной логики: логирование, метрики, короткое
замыкание (например auth → 401 без вызова хендлера).

```python
async def timing(req, next_handler):
    start = time.perf_counter()
    resp = await next_handler(req)
    print(f"{req.method} {req.path} занял {time.perf_counter() - start:.4f}s")
    return resp

app.use(timing)
```

Дальше — готовые contrib-адаптеры. Каждый подключается одной строкой и решает
одну конкретную задачу.

## CORS

Работает **в Rust** — без per-request расходов Python, включая preflight.
Origin сверяется со списком разрешённых, а не отдаётся всем подряд.

```python
from ferronit.contrib.cors import cors

app.use(cors(allow_origins=["https://app.example"], max_age=600))
```

Preflight (`OPTIONS`) отвечается нативно кодом `204`, если origin разрешён.

## Security-заголовки

```python
from ferronit.contrib.security import security_headers

app.use(security_headers())
app.use(security_headers(hsts=False, csp="default-src 'self'"))
```

Добавляет `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Strict-Transport-Security`, `Referrer-Policy` и опционально CSP. Пользователь
может переопределить любой заголовок (setdefault).

## Rate limiting

Скользящее окно в памяти; возвращает `429` с `Retry-After`. При
масштабировании на несколько воркеров нужен распределённый бэкенд (Redis) —
in-memory лимиты умножаются на число процессов.

```python
from ferronit.contrib.ratelimit import rate_limit

app.use(rate_limit(limit=100, window=60.0))                          # по IP
app.use(rate_limit(limit=5, window=60.0,
                   key=lambda req: req.get_header("x-api-key") or ""))
```

## Трассировка

```python
from ferronit.contrib.tracing import trace_middleware, current_trace_id

app.use(trace_middleware)          # извлекает/генерирует X-Trace-Id, ставит в ответ

# внутри хендлера:
tid = current_trace_id()
```

## Health checks

```python
from ferronit.contrib.health import HealthCheck

health = HealthCheck()
health.add("db", check_db)         # sync/async-функция → bool

@app.route("/health")
async def h(req):
    return await health(req)       # 200 или 503
```

`HealthCheck` собирает проверки зависимостей (БД, Kafka, ...) и отдаёт 200/503
вместе с деталями по каждой. Результат последнего прогона доступен через
свойство `is_healthy`.

## Статика

```python
from ferronit.contrib.staticfiles import StaticFiles

app.mount("/static", StaticFiles("public", cache_ttl=3600))
```

Отдаёт файлы с content type, кэшированием `ETag`/`Last-Modified` и поддержкой
range; блокирует path traversal, symlink-выход и dotfiles.
