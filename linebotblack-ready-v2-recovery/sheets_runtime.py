"""Process-local Sheets quota guard. No distributed quota/exactly-once claims."""
import threading
import time
from collections import deque


class SheetsBusy(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after) + 1)
        super().__init__('Sheets quota cooling down; retry later')


class QuotaGate:
    def __init__(self, limit=45):
        self.limit = limit
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.calls = {'read': deque(), 'write': deque()}
            self.blocked = {'read': 0.0, 'write': 0.0}

    def block(self, kind, seconds):
        with self.lock:
            self.blocked[kind] = max(self.blocked[kind], time.monotonic() + seconds)

    def take(self, kind, background=False):
        while True:
            now = time.monotonic()
            with self.lock:
                calls = self.calls[kind]
                while calls and calls[0] <= now - 60:
                    calls.popleft()
                wait = max(0.0, self.blocked[kind] - now)
                if len(calls) >= self.limit:
                    wait = max(wait, calls[0] + 60.1 - now)
                if wait <= 0:
                    calls.append(now)
                    return
            if not background:
                raise SheetsBusy(wait)
            # Only the bootstrap thread can wait; never stall a live webhook
            # for a whole quota window or busy-loop through failed requests.
            time.sleep(wait)
