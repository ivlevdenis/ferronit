# Middleware & contrib

## Middleware

A middleware is `(request, next_handler) -> response`, sync or async:

```python
async def timing(req, next_handler):
    start = time.perf_counter()
    resp = await next_handler(req)
    print(f"{req.method} {req.path} took {time.perf_counter() - start:.4f}s")
    return resp

app.use(timing)
```

## CORS

CORS runs **in Rust** — no per-request Python cost, including preflight:

```python
from ferrox.contrib.cors import cors

app.use(cors(allow_origins=["https://app.example"], max_age=600))
```

Preflight (`OPTIONS`) is answered natively with `204` when the origin is
allowed.

## Security headers

```python
from ferrox.contrib.security import security_headers

app.use(security_headers())
app.use(security_headers(hsts=False, csp="default-src 'self'"))
```

Adds `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Strict-Transport-Security`, `Referrer-Policy` and an optional CSP.

## Rate limiting

In-memory sliding window; returns `429` with `Retry-After`:

```python
from ferrox.contrib.ratelimit import rate_limit

app.use(rate_limit(limit=100, window=60.0))                          # per IP
app.use(rate_limit(limit=5, window=60.0,
                   key=lambda req: req.get_header("x-api-key") or ""))
```

## Tracing

```python
from ferrox.contrib.tracing import trace_middleware, current_trace_id

app.use(trace_middleware)          # extract/generate X-Trace-Id, set response header

# inside a handler:
tid = current_trace_id()
```

## Health checks

```python
from ferrox.contrib.health import HealthCheck

health = HealthCheck()
health.add("db", check_db)         # async callable → bool

@app.route("/health")
async def h(req):
    return await health(req)       # 200 or 503
```

## Static files

```python
from ferrox.contrib.staticfiles import StaticFiles

app.mount("/static", StaticFiles("public", cache_ttl=3600))
```

Serves files with content types, `ETag`/`Last-Modified` caching and range
support; blocks path traversal and dotfiles.
