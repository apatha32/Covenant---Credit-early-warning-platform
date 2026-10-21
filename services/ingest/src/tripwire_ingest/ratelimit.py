"""Token-bucket rate limiter and a sliding-window meter that proves the limit held."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class TokenBucket:
    """Allows `rate` acquisitions per second on average, with bursts of up to `capacity`.

    In any one-second window at most `capacity + rate` acquisitions can succeed.
    """

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("rate must be > 0 and capacity >= 1")
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a token is available. Returns the seconds spent waiting."""
        waited = 0.0
        with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return waited
                delay = (1 - self._tokens) / self.rate
                self._sleep(delay)
                waited += delay


class RateMeter:
    """Records event times and the most events seen in any one-second window."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._window: deque[float] = deque()
        self.max_per_second = 0

    def record(self) -> None:
        now = self._clock()
        self._window.append(now)
        while self._window and self._window[0] <= now - 1.0:
            self._window.popleft()
        self.max_per_second = max(self.max_per_second, len(self._window))
