#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc

    # Dev convenience: default `runserver` to port 8001 so it matches the
    # frontend's VITE_API_URL=http://localhost:8001/. Pass an explicit
    # `runserver <addr:port>` or set BACKEND_PORT to override.
    if (
        len(sys.argv) >= 2
        and sys.argv[1] == 'runserver'
        and not any(not arg.startswith('-') for arg in sys.argv[2:])
    ):
        sys.argv.append(os.environ.get('BACKEND_PORT', '8001'))

    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
