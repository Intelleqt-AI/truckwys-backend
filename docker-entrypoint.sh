#!/bin/sh
set -e

ROLE="${1:-web}"

case "$ROLE" in
  web)
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
    exec gunicorn config.asgi:application \
      -k uvicorn.workers.UvicornWorker \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers 1 \
      --timeout 120 \
      --access-logfile - --error-logfile - \
      --log-level debug
    ;;
  worker)
    exec celery -A config worker --loglevel=info --concurrency="${CELERY_CONCURRENCY:-2}"
    ;;
  beat)
    # Beat can wedge with the process still alive — single-threaded loop, one
    # publish blocked on a dead-but-not-closed socket, nothing logged. It
    # happened twice in production (05-07 and 08 Sept 2026): container "Up",
    # restarts=0, and no scheduled task for hours.
    #
    # A compose healthcheck alone does NOT fix that: plain Docker marks a
    # container unhealthy but never restarts it (only Swarm does), and
    # `restart: unless-stopped` reacts to exits, not to health. So supervise
    # it here — beat rewrites its schedule file every tick, and if that mtime
    # stops advancing we kill beat and exit non-zero, which is the exit
    # Docker's restart policy does act on.
    SCHEDULE_FILE="${CELERY_BEAT_SCHEDULE_FILENAME:-/var/run/celery/celerybeat-schedule}"
    # Must stay well clear of how often beat actually writes this file, or the
    # watchdog kills a healthy process. The first version used 180s against
    # PersistentScheduler's 180s sync timer and restarted beat 57 times in a
    # day, every time reporting 191-195s. CELERY_BEAT_SYNC_EVERY=1 now writes
    # it every ~20s, and 600s leaves a wide margin while still catching the
    # real failure — the stalls this exists for lasted 6h and 54h.
    STALL_SECONDS="${BEAT_STALL_SECONDS:-600}"
    mkdir -p "$(dirname "$SCHEDULE_FILE")"

    celery -A config beat --loglevel=info --schedule "$SCHEDULE_FILE" &
    BEAT_PID=$!

    # Forward stop signals so `docker stop` still shuts down cleanly.
    trap 'kill -TERM "$BEAT_PID" 2>/dev/null; exit 0' TERM INT

    while true; do
      sleep 60
      # Beat died on its own — let the restart policy handle it.
      kill -0 "$BEAT_PID" 2>/dev/null || {
        wait "$BEAT_PID"
        exit $?
      }
      # No schedule file yet: still starting up, give it another cycle.
      [ -f "$SCHEDULE_FILE" ] || continue
      AGE=$(( $(date +%s) - $(stat -c %Y "$SCHEDULE_FILE") ))
      if [ "$AGE" -gt "$STALL_SECONDS" ]; then
        echo "beat watchdog: schedule file untouched for ${AGE}s (limit ${STALL_SECONDS}s) — restarting" >&2
        kill -9 "$BEAT_PID" 2>/dev/null
        exit 1
      fi
    done
    ;;
  migrate)
    exec python manage.py migrate --noinput
    ;;
  *)
    exec "$@"
    ;;
esac
