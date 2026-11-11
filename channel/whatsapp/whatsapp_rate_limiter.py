"""Outbound pacing for the WhatsApp Cloud API.

Meta enforces two independent limits on a business number:

* throughput: messages per second for the whole number (error 130429);
* pair rate: roughly one message every 6 seconds to the same recipient, with
  short bursts tolerated (error 131056).

``SendThrottle`` keeps the channel under both. A send takes one token from the
recipient's bucket *and* one from the global bucket, atomically: both are taken
only when both are available, so a recipient that has to wait never sits on
global capacity. Bucket maths happens under one lock and waiting happens outside
it, so one slow recipient never holds up the others. Waiting is capped by
``max_wait`` because senders run on the process-wide reply thread pool. Past
the cap, ``ThrottleTimeout`` is raised rather than tying up a worker indefinitely.
"""

import threading
import time
from collections import OrderedDict


class ThrottleTimeout(Exception):
    """A send would have had to wait longer than ``max_wait``."""


class TokenBucket:
    """Classic token bucket. Not thread-safe on its own; ``SendThrottle`` locks it."""

    def __init__(self, capacity, refill_per_sec, clock=time.monotonic):
        if capacity <= 0 or refill_per_sec <= 0:
            raise ValueError("capacity and refill_per_sec must be positive")
        self._capacity = float(capacity)
        self._rate = float(refill_per_sec)
        self._clock = clock
        self._tokens = float(capacity)
        self._stamp = clock()

    def _refill(self):
        now = self._clock()
        elapsed = max(0.0, now - self._stamp)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._stamp = now

    def wait_time(self) -> float:
        """Seconds until one token is available (0.0 if available now). Takes nothing."""
        self._refill()
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self._rate

    def take(self) -> None:
        self._tokens -= 1.0

    def drain(self) -> None:
        self._refill()
        self._tokens = 0.0

    def try_acquire(self) -> float:
        """Take one token. Returns 0.0 on success, otherwise the seconds until one is available."""
        wait = self.wait_time()
        if wait == 0.0:
            self.take()
        return wait


class SendThrottle:
    """Global + per-recipient token buckets for one business phone number."""

    def __init__(self, global_rate, pair_interval, pair_burst, clock=time.monotonic,
                 sleep=time.sleep, max_pairs=10_000, max_wait=None):
        if pair_interval <= 0:
            raise ValueError("pair_interval must be positive")
        self._clock = clock
        self._sleep = sleep
        self._pair_burst = pair_burst
        self._pair_rate = 1.0 / float(pair_interval)
        self._max_pairs = max_pairs
        self._max_wait = max_wait
        # Capacity of at least one token, or a rate below 1/s could never send.
        self._global = TokenBucket(max(1.0, global_rate), global_rate, clock)
        # Validate pair settings eagerly instead of on the first send.
        TokenBucket(pair_burst, self._pair_rate, clock)
        self._pairs = OrderedDict()
        self._lock = threading.Lock()

    def pair_count(self) -> int:
        with self._lock:
            return len(self._pairs)

    def _pair_bucket(self, recipient) -> TokenBucket:
        bucket = self._pairs.get(recipient)
        if bucket is None:
            bucket = TokenBucket(self._pair_burst, self._pair_rate, self._clock)
            self._pairs[recipient] = bucket
            while len(self._pairs) > self._max_pairs:
                self._pairs.popitem(last=False)
        else:
            self._pairs.move_to_end(recipient)
        return bucket

    def try_acquire(self, recipient: str) -> float:
        """Non-blocking: take both tokens and return 0.0, or take nothing and return the wait."""
        with self._lock:
            pair = self._pair_bucket(recipient)
            wait = max(pair.wait_time(), self._global.wait_time())
            if wait == 0.0:
                pair.take()
                self._global.take()
            return wait

    def acquire(self, recipient: str) -> float:
        """Block until *recipient* may be sent one message. Returns the seconds waited."""
        waited = 0.0
        while True:
            wait = self.try_acquire(recipient)
            if wait == 0.0:
                return waited
            if self._max_wait is not None and waited + wait > self._max_wait:
                raise ThrottleTimeout(
                    f"recipient {recipient} would wait {waited + wait:.1f}s (> max_wait {self._max_wait}s)")
            self._sleep(wait)
            waited += wait

    def penalize(self, recipient: str) -> None:
        """Meta reported a pair-rate hit (131056): make the next send wait a full interval."""
        with self._lock:
            self._pair_bucket(recipient).drain()
