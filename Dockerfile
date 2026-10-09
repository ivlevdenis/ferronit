# syntax=docker/dockerfile:1
#
# Ferrox — единый пакет: Python (ferrox/ + ferrox_db/) и нативное ядро (ferrox_core + ferrox_core.db)
# собираются maturin-ом в один wheel (abi3-py312) прямо в билд-стейдже. Наружу нужна только
# сеть до PyPI/crates.io. Итоговый образ содержит один wheel и прод-сервер Granian.
#
#   docker build -t ferrox:0.8.1 .
#   docker run --rm -p 8000:8000 ferrox:0.8.1
#
# ─────────────────────────── Stage 1: wheel ───────────────────────────
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
# pyo3 0.24 не знает про новые интерпретаторы; для abi3-сборки флаг безопасен
ENV PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates build-essential pkg-config \
 && rm -rf /var/lib/apt/lists/*

RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /src

# maturin mixed-проект: корневой pyproject указывает manifest-path = ferrox-rs/Cargo.toml.
# ferrox-rs/target/ исключён .dockerignore; crates скачиваем отдельным слоем.
COPY ferrox-rs/ ./ferrox-rs/
COPY ferrox/ ./ferrox/
COPY ferrox_db/ ./ferrox_db/
COPY pyproject.toml README.md ./
RUN cargo fetch --manifest-path ferrox-rs/Cargo.toml
RUN pip install --no-cache-dir "maturin>=1.14,<2" \
 && maturin build --release --out /wheels -i python3

# ─────────────────────────── Stage 2: runtime ──────────────────────────
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    APP=demo_app:app \
    SERVER=granian \
    PORT=8000 \
    WORKERS=1 \
    HEALTH_PATH=/

COPY --from=builder /wheels /wheels
# Один wheel: pip install ferrox ставит и Python-слой, и нативное ядро.
RUN pip install --no-cache-dir "/wheels/$(cd /wheels && ls ferrox-[0-9]*.whl)[server,db,postgres,pydantic]" \
 && pip install --no-cache-dir aiosqlite uvloop \
 && rm -rf /wheels

# Прод-образ работает не от root
RUN useradd --create-home --uid 10001 ferrox

WORKDIR /app
COPY --chown=ferrox:ferrox docker/demo_app.py /app/demo_app.py
COPY --chown=ferrox:ferrox docker/entrypoint.sh /usr/local/bin/ferrox-entrypoint
# Бандл-примеры: demo_app (без зависимостей) и полный DDD e-commerce из examples/app.py
COPY --chown=ferrox:ferrox examples/app.py /app/examples/app.py
COPY --chown=ferrox:ferrox public/ /app/public/
RUN chmod +x /usr/local/bin/ferrox-entrypoint

USER ferrox
EXPOSE 8000

# Проверяем живое приложение end-to-end, а не только открытый порт.
# HEALTH_PATH — маршрут, который у вашего приложения отдаёт 200 (по умолчанию /).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
u='http://127.0.0.1:'+os.environ.get('PORT','8000')+os.environ.get('HEALTH_PATH','/'); \
sys.exit(0 if urllib.request.urlopen(u, timeout=2).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/ferrox-entrypoint"]
