"""WhatsApp send throttle: a global bucket per number plus one bucket per
recipient, so the channel stays under Meta's throughput and pair-rate limits."""
import threading

import pytest

from channel.whatsapp.whatsapp_rate_limiter import SendThrottle, TokenBucket


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_bucket_grants_up_to_capacity_then_reports_wait():
    clock = FakeClock()
    bucket = TokenBucket(capacity=2, refill_per_sec=1.0, clock=clock)
    assert bucket.try_acquire() == 0.0
    assert bucket.try_acquire() == 0.0
    assert bucket.try_acquire() == pytest.approx(1.0)


def test_bucket_refills_over_time_without_exceeding_capacity():
    clock = FakeClock()
    bucket = TokenBucket(capacity=2, refill_per_sec=0.5, clock=clock)
    bucket.try_acquire()
    bucket.try_acquire()
    clock.now += 100  # long idle must not bank more than capacity
    assert bucket.try_acquire() == 0.0
    assert bucket.try_acquire() == 0.0
    assert bucket.try_acquire() == pytest.approx(2.0)


def test_refused_acquire_takes_no_token():
    clock = FakeClock()
    bucket = TokenBucket(capacity=1, refill_per_sec=1.0, clock=clock)
    bucket.try_acquire()
    assert bucket.try_acquire() > 0
    clock.now += 1.0
    assert bucket.try_acquire() == 0.0


@pytest.mark.parametrize("capacity,rate", [(0, 1.0), (1, 0), (-1, 1.0)])
def test_bucket_rejects_non_positive_settings(capacity, rate):
    with pytest.raises(ValueError):
        TokenBucket(capacity=capacity, refill_per_sec=rate)


def _throttle(clock, **kw):
    params = dict(global_rate=100, pair_interval=6, pair_burst=2)
    params.update(kw)
    return SendThrottle(clock=clock, sleep=clock.sleep, **params)


def test_pair_burst_then_waits_pair_interval():
    clock = FakeClock()
    throttle = _throttle(clock)
    assert throttle.acquire("u1") == 0.0
    assert throttle.acquire("u1") == 0.0
    waited = throttle.acquire("u1")
    assert waited == pytest.approx(6.0)


def test_recipients_are_paced_independently():
    clock = FakeClock()
    throttle = _throttle(clock, pair_burst=1)
    assert throttle.acquire("u1") == 0.0
    assert throttle.acquire("u2") == 0.0
    assert clock.slept == []


def test_global_rate_caps_across_recipients():
    clock = FakeClock()
    throttle = _throttle(clock, global_rate=2, pair_burst=10)
    throttle.acquire("a")
    throttle.acquire("b")
    waited = throttle.acquire("c")
    assert waited == pytest.approx(0.5)


def test_pair_buckets_are_bounded_lru():
    clock = FakeClock()
    throttle = _throttle(clock, pair_burst=1, max_pairs=2)
    throttle.acquire("a")
    throttle.acquire("b")
    throttle.acquire("c")  # evicts "a"
    assert throttle.pair_count() == 2
    # "a" was evicted, so it starts with a fresh burst again
    assert throttle.acquire("a") == 0.0


def test_sleep_happens_outside_the_lock():
    """A recipient waiting out its pair interval must not block other recipients."""
    throttle = SendThrottle(global_rate=1000, pair_interval=60, pair_burst=1)
    throttle.acquire("slow")
    blocked = threading.Thread(target=throttle.acquire, args=("slow",), daemon=True)
    blocked.start()
    done = threading.Event()

    def other():
        throttle.acquire("fast")
        done.set()

    threading.Thread(target=other, daemon=True).start()
    assert done.wait(2.0), "another recipient was blocked by a sleeping one"


def test_penalize_drains_recipient_so_next_send_waits_full_interval():
    """After a 131056 from Meta, the next try must wait out the pair window."""
    clock = FakeClock()
    throttle = _throttle(clock, pair_burst=5)
    throttle.acquire("u")
    throttle.penalize("u")
    assert throttle.acquire("u") == pytest.approx(6.0)


def test_waiting_recipient_holds_no_global_token():
    """A recipient blocked on its pair bucket must not consume global capacity."""
    clock = FakeClock()
    throttle = _throttle(clock, global_rate=1, pair_burst=1)
    throttle.acquire("u")          # takes the only global token at t=0
    clock.now += 1.0               # global refilled, u's pair still empty for 5s
    assert throttle.try_acquire("u") == pytest.approx(5.0)  # refused: takes nothing
    # so the refilled global token is still there for another recipient
    assert throttle.try_acquire("v") == 0.0


def test_max_wait_raises_instead_of_blocking_forever():
    from channel.whatsapp.whatsapp_rate_limiter import ThrottleTimeout

    clock = FakeClock()
    throttle = _throttle(clock, pair_burst=1, pair_interval=600, max_wait=10)
    throttle.acquire("u")
    with pytest.raises(ThrottleTimeout):
        throttle.acquire("u")
    assert sum(clock.slept) <= 10


def test_fractional_global_rate_still_sends():
    clock = FakeClock()
    throttle = _throttle(clock, global_rate=0.5, pair_burst=10, max_wait=10)
    assert throttle.acquire("a") == 0.0
    assert throttle.acquire("b") == pytest.approx(2.0)
