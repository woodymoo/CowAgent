"""Outbound message pipeline: throttle → Graph API → backoff on throttling.

Each attempt, retries included, first waits for ``SendThrottle``, so a retry
storm can never get past the pacing. Only errors Meta documents as transient
are retried. Quality signals (131048 spam rate limit) and the closed 24-hour
window (131047) fail at once, because retrying them hurts the number's standing.
"""

import random
import time

from channel.whatsapp.whatsapp_api import (
    CODE_PAIR_RATE_LIMIT,
    CODE_SPAM_RATE_LIMIT,
    CODE_WINDOW_CLOSED,
    WhatsAppApiError,
)
from common.log import logger

RECIPIENT_TYPES = ("individual", "group")

_OPERATOR_HINTS = {
    CODE_SPAM_RATE_LIMIT: "Meta flagged the number for spam-like sending (131048). Slow down and "
                          "review message quality; repeated hits can restrict the number.",
    CODE_WINDOW_CLOSED: "The 24-hour customer service window is closed (131047). Free-form "
                        "messages are only allowed within 24h of the user's last message.",
}


class WhatsAppSender:
    def __init__(self, client, throttle, max_attempts=3, base_delay=1.0, max_delay=30.0,
                 sleep=time.sleep, rand=random.random):
        self._client = client
        self._throttle = throttle
        self._max_attempts = max(1, int(max_attempts))
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._rand = rand

    def send(self, to: str, recipient_type: str, msg_type: str, body: dict) -> dict:
        if recipient_type not in RECIPIENT_TYPES:
            raise ValueError(f"unknown recipient_type: {recipient_type!r}")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": recipient_type,
            "to": to,
            "type": msg_type,
            msg_type: body,
        }
        attempt = 0
        while True:
            self._throttle.acquire(to)
            try:
                return self._client.send_message(payload)
            except WhatsAppApiError as e:
                attempt += 1
                hint = _OPERATOR_HINTS.get(e.code)
                if hint:
                    logger.error(f"[WhatsApp] {hint} to={to}")
                if not e.retryable or attempt >= self._max_attempts:
                    raise
                if e.code == CODE_PAIR_RATE_LIMIT:
                    # Our pacing was too fast for this recipient. Drain its bucket
                    # so the next acquire() waits out a full pair interval.
                    self._throttle.penalize(to)
                delay = min(self._max_delay, self._base_delay * (2 ** (attempt - 1)) * (0.5 + self._rand()))
                logger.warning(f"[WhatsApp] send throttled/transient error ({e}); "
                               f"retry {attempt}/{self._max_attempts - 1} in {delay:.1f}s")
                self._sleep(delay)
