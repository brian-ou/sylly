"""Simple in-memory per-user rate limiter using a sliding window.

NOTE: This is a process-local dict; counts do NOT survive restarts and do not
work across multiple worker processes. For production use, swap in Redis.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict


class SlidingWindowRateLimiter:
    """Track a sliding window of timestamps per key."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """Return True if request is allowed; False if rate-limited.

        Records the current timestamp on success.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        """Clear all hits, or hits for a specific key."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# Default parser limiter: 10 calls per hour per user
parse_limiter = SlidingWindowRateLimiter(limit=10, window_seconds=3600)

# Chat limiter: 60 calls per hour per user
chat_limiter = SlidingWindowRateLimiter(limit=60, window_seconds=3600)
