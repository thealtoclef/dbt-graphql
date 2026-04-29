# syntax=docker/dockerfile:1.7

# ---- Builder ---------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:trixie AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/venv

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --no-editable

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- Runtime ---------------------------------------------------------------
FROM gcr.io/distroless/python3-debian13:nonroot

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY --from=builder --chown=nonroot:nonroot /venv /venv

WORKDIR /app

ENTRYPOINT ["/venv/bin/dbt-graphql"]
