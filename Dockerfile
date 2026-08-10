FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    STUDIO_DATA_DIR=/data \
    SENSE_NOVA_LOCAL_HOST=0.0.0.0 \
    SENSE_NOVA_LOCAL_PORT=8001

WORKDIR /app
COPY . /app

RUN set -eu; \
    engine=/app/inference; \
    uv sync --project /app/studio --frozen; \
    uv sync --project "$engine" --frozen; \
    "$engine/.venv/bin/python" -m playwright install --with-deps chromium; \
    python /app/scripts/launch.py --check --no-browser-install

EXPOSE 8001
VOLUME ["/data"]

CMD ["python", "scripts/launch.py", "--host", "0.0.0.0", "--port", "8001", "--no-install", "--no-browser-install"]
