# syntax=docker/dockerfile:1.7
# Base images are build args so mirrored or proxied registries can be used, e.g.
#   docker build --build-arg UV_IMAGE=mirror/uv:0.11 --build-arg PYTHON_IMAGE=mirror/python:3.12-slim .
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM ${PYTHON_IMAGE}
RUN useradd --create-home --uid 10001 maw
WORKDIR /app
COPY --from=build --chown=maw:maw /app/.venv /app/.venv
COPY --chown=maw:maw src ./src
COPY --chown=maw:maw workflows ./workflows
COPY --chown=maw:maw config ./config
COPY --chown=maw:maw mocks ./mocks
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MAW_HOST=0.0.0.0 MAW_PORT=8000 \
    MAW_WORKFLOWS_DIR=/app/workflows MAW_CONNECTIONS_FILE=/app/config/connections.yaml
USER maw
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"
CMD ["maw", "serve"]
