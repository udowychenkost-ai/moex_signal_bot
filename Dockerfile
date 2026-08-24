FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG GIT_COMMIT=unknown
ARG APP_VERSION=0.4.0
ENV GIT_COMMIT=${GIT_COMMIT} \
    APP_VERSION=${APP_VERSION}

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --upgrade pip && pip install .

RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python", "-m", "app", "healthcheck"]

CMD ["python", "-m", "app", "run"]
