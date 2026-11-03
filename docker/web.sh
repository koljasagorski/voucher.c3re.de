#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py createcachetable
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --access-logfile - \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
