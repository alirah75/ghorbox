FROM python:3.13-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" DATA_DIR=/var/lib/feedback SECRETS_DIR=/run/feedback-keys
RUN groupadd --gid 10001 feedback && useradd --uid 10001 --gid 10001 --no-create-home feedback
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["python", "-m", "app.main"]
