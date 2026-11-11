"""WhatsAppSender: every attempt waits for the throttle, throttling errors are
retried with exponential backoff, and quality and window errors are never retried."""
import pytest

from channel.whatsapp.whatsapp_api import WhatsAppApiError
from channel.whatsapp.whatsapp_sender import WhatsAppSender


class FakeThrottle:
    def __init__(self):
        self.acquired = []
        self.penalized = []

    def penalize(self, recipient):
        self.penalized.append(recipient)

    def acquire(self, recipient):
        self.acquired.append(recipient)
        return 0.0


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.payloads = []

    def send_message(self, payload):
        self.payloads.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _sender(client, throttle=None, sleeps=None, **kw):
    sleeps = sleeps if sleeps is not None else []
    return WhatsAppSender(client, throttle or FakeThrottle(), sleep=sleeps.append,
                          rand=lambda: 0.5, **kw)


def test_builds_individual_text_payload():
    client = FakeClient({"messages": [{"id": "wamid.1"}]})
    throttle = FakeThrottle()
    out = _sender(client, throttle).send("15551234567", "individual", "text", {"body": "hi"})
    assert client.payloads == [{
        "messaging_product": "whatsapp", "recipient_type": "individual",
        "to": "15551234567", "type": "text", "text": {"body": "hi"},
    }]
    assert throttle.acquired == ["15551234567"]
    assert out["messages"][0]["id"] == "wamid.1"


def test_group_payload():
    client = FakeClient({})
    _sender(client).send("GRP", "group", "image", {"id": "M"})
    assert client.payloads[0]["recipient_type"] == "group"
    assert client.payloads[0]["to"] == "GRP"
    assert client.payloads[0]["image"] == {"id": "M"}


def test_rejects_unknown_recipient_type():
    with pytest.raises(ValueError):
        _sender(FakeClient()).send("x", "broadcast", "text", {"body": "x"})


def test_retries_throttling_with_exponential_backoff_and_reacquires():
    client = FakeClient(WhatsAppApiError(400, code=130429), WhatsAppApiError(400, code=131056), {"ok": 1})
    throttle = FakeThrottle()
    sleeps = []
    out = _sender(client, throttle, sleeps, base_delay=1.0).send("u", "individual", "text", {"body": "x"})
    assert out == {"ok": 1}
    assert sleeps == [1.0, 2.0]          # base * 2**n * (0.5 + 0.5)
    assert throttle.acquired == ["u", "u", "u"]  # every attempt is paced


def test_gives_up_after_max_attempts():
    errors = [WhatsAppApiError(503) for _ in range(3)]
    client = FakeClient(*errors)
    with pytest.raises(WhatsAppApiError):
        _sender(client, max_attempts=3).send("u", "individual", "text", {"body": "x"})
    assert len(client.payloads) == 3


@pytest.mark.parametrize("code", [131048, 131047, 100])
def test_non_retryable_errors_raise_immediately(code):
    client = FakeClient(WhatsAppApiError(400, code=code))
    sleeps = []
    with pytest.raises(WhatsAppApiError):
        _sender(client, sleeps=sleeps).send("u", "individual", "text", {"body": "x"})
    assert len(client.payloads) == 1 and sleeps == []


def test_backoff_is_capped():
    client = FakeClient(*[WhatsAppApiError(429) for _ in range(5)], {})
    sleeps = []
    _sender(client, sleeps=sleeps, max_attempts=6, base_delay=10, max_delay=30).send(
        "u", "individual", "text", {"body": "x"})
    assert max(sleeps) <= 30


def test_pair_rate_error_penalizes_recipient_bucket():
    """131056 means our pacing was too fast for this recipient: drain its bucket
    so the retry waits a full pair interval rather than the short backoff alone."""
    client = FakeClient(WhatsAppApiError(400, code=131056), {})
    throttle = FakeThrottle()
    _sender(client, throttle).send("u", "individual", "text", {"body": "x"})
    assert throttle.penalized == ["u"]


def test_other_throttling_does_not_penalize_pair():
    client = FakeClient(WhatsAppApiError(400, code=130429), {})
    throttle = FakeThrottle()
    _sender(client, throttle).send("u", "individual", "text", {"body": "x"})
    assert throttle.penalized == []
