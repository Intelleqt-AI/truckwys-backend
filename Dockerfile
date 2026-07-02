# TruckWys backend — production image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Build deps for psycopg2 / Pillow / reportlab are covered by wheels; curl for healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Migrations + collectstatic run at startup (env is present then, so the
# SECRET_KEY fail-fast won't trip during build).
RUN chmod +x docker-entrypoint.sh \
    && useradd -m appuser \
    && mkdir -p staticfiles media \
    && chown -R appuser /app
USER appuser

EXPOSE $PORT

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["web"]
