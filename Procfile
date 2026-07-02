release: python manage.py migrate --noinput && python manage.py seed_toll_data
web: gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 120
