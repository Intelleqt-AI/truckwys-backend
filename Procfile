release: python manage.py migrate --noinput && python manage.py seed_toll_data
web: daphne -b 0.0.0.0 -p ${PORT:-8000} config.asgi:application
