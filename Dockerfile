# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install .


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="meny-mcp" \
      org.opencontainers.image.description="MCP server for meny.no: grocery search, price-per-unit optimisation and basket building" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin meny \
 && mkdir -p /data && chown meny:meny /data
USER meny
WORKDIR /home/meny

# The weekly-menu manifest lives here. A container's filesystem is ephemeral,
# so mount a volume on /data or a redeploy loses a run mid-flight - and the
# manifest exists precisely so a run survives losing its state.
VOLUME ["/data"]

ENV MENY_CHAIN=meny \
    MENY_STORE_ID=7080001150488 \
    MENY_MAX_CONCURRENCY=4 \
    MENY_MIN_INTERVAL=0.12 \
    MENY_TIMEOUT=20 \
    MENY_TRANSPORT=http \
    MENY_HTTP_HOST=0.0.0.0 \
    MENY_HTTP_PORT=8000 \
    MENY_HTTP_PATH=/mcp \
    MENY_STATE_DIR=/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "\
import os,socket,sys;\
t=os.environ.get('MENY_TRANSPORT','stdio');\
sys.exit(socket.socket().connect_ex(('127.0.0.1',int(os.environ.get('MENY_HTTP_PORT','8000'))))) \
    if t in ('http','streamable-http','sse') else \
__import__('meny_mcp.config',fromlist=['settings'])"

ENTRYPOINT ["/usr/bin/tini", "--", "meny-mcp"]
