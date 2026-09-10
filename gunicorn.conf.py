# V1.6.17G safety net for slower Google Sheet / summary operations.
# Gunicorn auto-loads ./gunicorn.conf.py when started from the repository root.
timeout = 120
graceful_timeout = 30
keepalive = 5
