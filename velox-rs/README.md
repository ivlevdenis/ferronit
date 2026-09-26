# velox-core

Native Rust core for the [Velox](../README.md) ASGI web framework.

Собирается maturin-ом в отдельный дистрибутив и импортируется как top-level модуль
`velox_core` — под этим именем его ждут `velox/core/app.py`, `request.py`, `response.py`.

## Что внутри

- `Router` — matchit-роутер: регистрация маршрутов, `lookup`/`resolve` без копий строк
- `Request` — ленивый парсер заголовков, query (UTF-8-корректный `url_decode`), JSON
- `Response` — `json` (сериализация напрямую в буфер, без промежуточного `serde_json::Value`),
  `text`, gzip-сжатие, экранирование `<`/`>`/`&` в JSON
- `VeloxApp` — ASGI-приложение: маршрутизация, CORS-preflight, gzip по q-факторам

## Сборка

```bash
# из корня репозитория
./scripts/build_packages.sh          # оба дистрибутива в dist/

# только ядро, для разработки (ставит .so в активный venv)
cd velox-rs && PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 maturin develop --release
```

Сборка идёт с `abi3-py312`: один wheel (`cp312-abi3`) работает на всех CPython от 3.12,
включая 3.14 — матрица версий Python не нужна.
