FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY prompts ./prompts
COPY fixtures ./fixtures
COPY evals ./evals
COPY icp.yaml ./
RUN uv sync --frozen --no-dev

# Migrations run as a separate compose service; the default command is the CLI help.
ENTRYPOINT ["sdr-loop"]
CMD ["--help"]
