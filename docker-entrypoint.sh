#!/bin/sh
set -e

ROLE="${1:-web}"

case "$ROLE" in
  web)
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
    exec daphne -b 0.0.0.0 -p "${PORT:-8000}" config.asgi:application
    ;;
  worker)
    exec celery -A config worker --loglevel=info --concurrency="${CELERY_CONCURRENCY:-2}"
    ;;
  beat)
    exec celery -A config beat --loglevel=info
    ;;
  migrate)
    exec python manage.py migrate --noinput
    ;;
  *)
    exec "$@"
    ;;
esac
