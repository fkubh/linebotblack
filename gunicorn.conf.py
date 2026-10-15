import os

bind = "0.0.0.0:" + os.environ.get("PORT", "10000")
# Shared mutable stores and synchronous Sheets operations require serial handling.
workers = 1
worker_class = "sync"
threads = 1
preload_app = False
reload = False
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
