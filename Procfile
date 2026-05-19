# Railway / Heroku: PORT is injected. Use 1 worker so only one Telegram poll + one state writer.
web: gunicorn pluxo_backend:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --access-logfile -
