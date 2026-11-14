# syntax=docker/dockerfile:1
#
# Ferronit is a single package: Python (ferronit/) and the native core (ferronit_core)
# are built by maturin into one wheel (abi3-py312) right in the build stage. Only
# network access to PyPI/crates.io is required. The final image ships one wheel and Granian.
#
#   docker build -t ferronit:0.25.0 .
#   docker run --rm -p 8000:8000 ferronit:0.25.0
#
# ─────────────────────────── Stage 1: wheel ───────────────────────────
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
# pyo3 0.24 does not know about newer interpreters; the flag is safe for an abi3 build
ENV PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates build-essential pkg-config \
 && rm -rf /var/lib/apt/lists/*

RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /src

# maturin mixed project: the root pyproject sets manifest-path = ferronit-rs/Cargo.toml.
# ferronit-rs/target/ is excluded by .dockerignore; crates are downloaded in a separate layer.
COPY ferronit-rs/ ./ferronit-rs/
COPY ferronit/ ./ferronit/
COPY pyproject.toml README.md ./
RUN cargo fetch --manifest-path ferronit-rs/Cargo.toml
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
# One wheel: pip install ferronit brings both the Python layer and the native core.
RUN pip install --no-cache-dir "/wheels/$(cd /wheels && ls ferronit-[0-9]*.whl)[server,pydantic]" \
 && pip install --no-cache-dir uvloop \
 && rm -rf /wheels

# The production image does not run as root
RUN useradd --create-home --uid 10001 ferronit

WORKDIR /app
COPY --chown=ferronit:ferronit docker/demo_app.py /app/demo_app.py
COPY --chown=ferronit:ferronit docker/entrypoint.sh /usr/local/bin/ferronit-entrypoint
# Bundled examples: demo_app (dependency-free) and the full DDD e-commerce from examples/app.py
COPY --chown=ferronit:ferronit examples/app.py /app/examples/app.py
COPY --chown=ferronit:ferronit public/ /app/public/
RUN chmod +x /usr/local/bin/ferronit-entrypoint

USER ferronit
EXPOSE 8000

# Probe the live application end-to-end, not just an open port.
# HEALTH_PATH — the route your application answers with 200 (default: /).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
u='http://127.0.0.1:'+os.environ.get('PORT','8000')+os.environ.get('HEALTH_PATH','/'); \
sys.exit(0 if urllib.request.urlopen(u, timeout=2).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/ferronit-entrypoint"]
