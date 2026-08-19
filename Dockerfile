FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --upgrade pip && pip install .

RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "app", "run"]
