"""WhatsApp webhook protocol: Meta's verify handshake, the X-Hub-Signature-256
check over the raw body, and parsing a payload into typed events with the
schema checked at the boundary."""
import hashlib
import hmac
import json

import pytest

from channel.whatsapp.whatsapp_webhook import (
    WebhookSchemaError,
    parse_webhook,
    verify_signature,
    verify_subscription,
)

PNID = "1111"


def _sign(secret, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(value, field="messages"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba", "changes": [{"field": field, "value": value}]}],
    }


def _messages_value(messages, contacts=None, pnid=PNID):
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": pnid},
        "contacts": contacts or [{"profile": {"name": "Alice"}, "wa_id": "15551234567"}],
        "messages": messages,
    }


# -- handshake ---------------------------------------------------------------

def test_subscription_echoes_challenge_on_match():
    assert verify_subscription("subscribe", "tok", "123", "tok") == "123"


@pytest.mark.parametrize("mode,token,expected", [
    ("subscribe", "wrong", "tok"),
    ("unsubscribe", "tok", "tok"),
    ("subscribe", "", ""),  # an unset verify token must never match an empty one
    (None, None, "tok"),
    ("subscribe", "\u00e9", "tok"),  # non-ASCII must be a clean refusal, not a TypeError
])
def test_subscription_rejects(mode, token, expected):
    assert verify_subscription(mode, token, "123", expected) is None


# -- signature ---------------------------------------------------------------

def test_signature_valid():
    body = b'{"a":1}'
    assert verify_signature("s3cret", body, _sign("s3cret", body))


@pytest.mark.parametrize("header", [
    None, "", "sha256=deadbeef", "sha1=abc", _sign("other", b'{"a":1}'), "sha256=\u00e9",
])
def test_signature_invalid(header):
    assert not verify_signature("s3cret", b'{"a":1}', header)


def test_signature_requires_secret():
    body = b"{}"
    assert not verify_signature("", body, _sign("", body))


def test_signature_is_over_raw_bytes_not_reserialized_json():
    body = b'{"a": 1,  "b":2}'
    assert verify_signature("k", body, _sign("k", body))
    assert not verify_signature("k", json.dumps(json.loads(body)).encode(), _sign("k", body))


# -- schema ------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    None, [], {"object": "page", "entry": []}, {"object": "whatsapp_business_account"},
    {"object": "whatsapp_business_account", "entry": "x"},
])
def test_schema_rejects_non_whatsapp_payloads(payload):
    with pytest.raises(WebhookSchemaError):
        parse_webhook(payload, PNID)


def test_malformed_entries_are_skipped_not_fatal():
    good = _messages_value([{"from": "15551234567", "id": "wamid.1", "timestamp": "1",
                             "type": "text", "text": {"body": "hi"}}])
    payload = {"object": "whatsapp_business_account", "entry": [
        "junk", {"changes": "junk"}, {"changes": [None, {"field": "messages", "value": good}]},
    ]}
    batch = parse_webhook(payload, PNID)
    assert [m.msg_id for m in batch.messages] == ["wamid.1"]


# -- messages ----------------------------------------------------------------

def test_text_message_one_to_one():
    batch = parse_webhook(_payload(_messages_value([{
        "from": "15551234567", "id": "wamid.1", "timestamp": "1700000000",
        "type": "text", "text": {"body": "hello"},
    }])), PNID)
    (m,) = batch.messages
    assert m.msg_id == "wamid.1"
    assert m.msg_type == "text"
    assert m.text == "hello"
    assert m.sender_id == "15551234567"
    assert m.sender_name == "Alice"
    assert m.group_id is None
    assert m.timestamp == 1700000000


def test_group_message_uses_from_user_id_when_phone_hidden():
    batch = parse_webhook(_payload(_messages_value(
        [{"from_user_id": "BSUID.9", "group_id": "GRP1", "id": "wamid.2", "timestamp": "1",
          "type": "text", "text": {"body": "yo"}}],
        contacts=[{"profile": {"name": "Bob"}, "user_id": "BSUID.9"}],
    )), PNID)
    (m,) = batch.messages
    assert m.group_id == "GRP1"
    assert m.sender_id == "BSUID.9"
    assert m.sender_name == "Bob"


@pytest.mark.parametrize("mtype,body,expect", [
    ("image", {"id": "MID", "mime_type": "image/jpeg", "caption": "look"},
     {"media_id": "MID", "mime_type": "image/jpeg", "caption": "look"}),
    ("audio", {"id": "MID", "mime_type": "audio/ogg; codecs=opus", "voice": True},
     {"media_id": "MID", "mime_type": "audio/ogg; codecs=opus"}),
    ("document", {"id": "MID", "mime_type": "application/pdf", "filename": "a.pdf"},
     {"media_id": "MID", "filename": "a.pdf"}),
    ("video", {"id": "MID", "mime_type": "video/mp4"}, {"media_id": "MID"}),
])
def test_media_messages(mtype, body, expect):
    batch = parse_webhook(_payload(_messages_value([{
        "from": "15551234567", "id": "wamid.3", "timestamp": "1", "type": mtype, mtype: body,
    }])), PNID)
    (m,) = batch.messages
    assert m.msg_type == mtype
    for key, value in expect.items():
        assert getattr(m, key) == value


def test_message_without_id_or_sender_is_skipped():
    batch = parse_webhook(_payload(_messages_value([
        {"from": "1", "timestamp": "1", "type": "text", "text": {"body": "no id"}},
        {"id": "wamid.x", "timestamp": "1", "type": "text", "text": {"body": "no sender"}},
    ])), PNID)
    assert batch.messages == ()


def test_other_phone_number_is_ignored():
    batch = parse_webhook(_payload(_messages_value([{
        "from": "1", "id": "wamid.4", "timestamp": "1", "type": "text", "text": {"body": "x"},
    }], pnid="9999")), PNID)
    assert batch.messages == ()


# -- statuses and group events ----------------------------------------------

def test_status_events():
    value = {"metadata": {"phone_number_id": PNID}, "statuses": [{
        "id": "wamid.5", "status": "failed", "recipient_id": "1555",
        "errors": [{"code": 131047, "title": "Re-engagement message"}],
    }]}
    batch = parse_webhook(_payload(value), PNID)
    (s,) = batch.statuses
    assert (s.msg_id, s.status, s.recipient_id) == ("wamid.5", "failed", "1555")
    assert s.errors[0]["code"] == 131047


def test_group_events_are_collected():
    value = {"metadata": {"phone_number_id": PNID}, "groups": [{"type": "group_create",
                                                                "group_id": "G"}]}
    batch = parse_webhook(_payload(value, field="group_lifecycle_update"), PNID)
    (e,) = batch.group_events
    assert e.field == "group_lifecycle_update"
    assert e.value["groups"][0]["group_id"] == "G"


def test_unknown_fields_are_ignored():
    batch = parse_webhook(_payload({"x": 1}, field="account_update"), PNID)
    assert batch.messages == () and batch.statuses == () and batch.group_events == ()
