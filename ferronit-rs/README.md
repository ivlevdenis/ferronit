# ferronit-core

Native Rust core for the [Ferronit](../README.md) ASGI web framework.

Built by maturin into a separate distribution and imported as the top-level module
`ferronit._core` — that is the name `ferronit/core/app.py` and `request.py` expect.

## What's inside

- `Router` — matchit router: route registration, `lookup`/`resolve` without string copies
- `Request` — lazy parser for headers, query (UTF-8-correct `url_decode`), multipart
- `Response` — `text`, gzip compression
- `FerronitApp` — ASGI application: routing, CORS preflight, gzip by q-factors

## Building

```bash
# from the repository root
./scripts/build_packages.sh          # both distributions into dist/

# core only, for development (installs the .so into the active venv)
cd ferronit-rs && PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 maturin develop --release
```

The build uses `abi3-py312`: one wheel (`cp312-abi3`) works on every CPython from 3.12,
including 3.15 — no Python version matrix needed.
