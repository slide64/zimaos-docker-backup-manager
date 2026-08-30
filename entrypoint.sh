#!/bin/sh
set -eu

mkdir -p /data/logs /data/tmp
python /app/app.py --init-only

# cron relit automatiquement /etc/cron.d ; le fichier sera créé par l'app.
cron

exec gunicorn --bind 0.0.0.0:9876 --workers 1 --threads 4 --timeout 120 app:app
