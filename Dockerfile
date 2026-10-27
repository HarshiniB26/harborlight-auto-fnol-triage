# Python 3.12 workspace with uv.
# The default image is the devcontainer (sleep infinity).
# Production builds the runtime stage: docker build --target runtime.
FROM python:3.12-slim-bookworm AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY . .

RUN uv sync --locked \
    && chmod +x /app/infra/docker/entrypoint.sh

ENTRYPOINT ["/app/infra/docker/entrypoint.sh"]

FROM runtime AS dev

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT []
CMD ["sleep", "infinity"]
