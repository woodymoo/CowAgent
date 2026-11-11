"""WhatsApp Graph API client: the transport layer. It builds URLs, attaches the
bearer token, and turns Graph error envelopes into WhatsAppApiError."""
import pytest

from channel.whatsapp.whatsapp_api import WhatsAppApiClient, WhatsAppApiError


class FakeResponse:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else ("" if body is None else str(body))
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _client(session, **kw):
    return WhatsAppApiClient("TOKEN", "PNID", api_version="v21.0", session=session, **kw)


def test_send_message_posts_to_phone_number_messages():
    session = FakeSession(FakeResponse(body={"messages": [{"id": "wamid.1"}]}))
    out = _client(session).send_message({"to": "1", "type": "text"})
    method, url, kw = session.calls[0]
    assert method == "POST"
    assert url == "https://graph.facebook.com/v21.0/PNID/messages"
    assert kw["headers"]["Authorization"] == "Bearer TOKEN"
    assert kw["json"] == {"to": "1", "type": "text"}
    assert "timeout" in kw
    assert out["messages"][0]["id"] == "wamid.1"


def test_graph_error_envelope_becomes_typed_error():
    session = FakeSession(FakeResponse(400, body={"error": {
        "code": 131047, "message": "Re-engagement message", "error_user_title": "Window closed",
        "error_data": {"details": "more than 24 hours"},
    }}))
    with pytest.raises(WhatsAppApiError) as exc:
        _client(session).send_message({})
    err = exc.value
    assert (err.http_status, err.code) == (400, 131047)
    assert err.title == "Window closed"
    assert err.details == "more than 24 hours"
    assert not err.retryable


@pytest.mark.parametrize("status,code,retryable", [
    (400, 130429, True),   # throughput
    (400, 131056, True),   # pair rate
    (400, 80007, True),    # WABA rate limit
    (429, None, True),
    (503, None, True),
    (400, 131048, False),  # spam rate limit: quality signal, never hammer
    (400, 131047, False),  # 24h window
    (401, 190, False),     # bad token
])
def test_retryable_classification(status, code, retryable):
    body = {"error": {"code": code, "message": "m"}} if code else None
    session = FakeSession(FakeResponse(status, body=body, text="oops"))
    with pytest.raises(WhatsAppApiError) as exc:
        _client(session).send_message({})
    assert exc.value.retryable is retryable


def test_error_message_never_leaks_token():
    session = FakeSession(FakeResponse(500, text="bad token TOKEN in request"))
    with pytest.raises(WhatsAppApiError) as exc:
        _client(session).send_message({})
    assert "TOKEN" not in str(exc.value)


@pytest.mark.parametrize("exc_name,retryable", [
    ("ConnectionError", True),   # request never left: safe to retry
    ("ConnectTimeout", True),
    ("ReadTimeout", False),      # Meta may have accepted it: a retry could duplicate
])
def test_network_errors_retry_only_when_request_never_left(exc_name, retryable):
    import requests

    class Boom:
        def request(self, *a, **k):
            raise getattr(requests, exc_name)("x")

    with pytest.raises(WhatsAppApiError) as exc:
        _client(Boom()).send_message({})
    assert exc.value.retryable is retryable


def test_upload_media_sends_multipart_and_returns_id(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG")
    session = FakeSession(FakeResponse(body={"id": "MEDIA1"}))
    media_id = _client(session).upload_media(str(f), "image/png")
    method, url, kw = session.calls[0]
    assert (method, url) == ("POST", "https://graph.facebook.com/v21.0/PNID/media")
    assert kw["data"] == {"messaging_product": "whatsapp", "type": "image/png"}
    name, _fh, mime = kw["files"]["file"]
    assert (name, mime) == ("a.png", "image/png")
    assert media_id == "MEDIA1"


def test_upload_media_without_id_is_an_error(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    with pytest.raises(WhatsAppApiError):
        _client(FakeSession(FakeResponse(body={}))).upload_media(str(f), "image/png")


def test_get_media_url():
    session = FakeSession(FakeResponse(body={"url": "https://lookaside.example/x", "id": "M"}))
    assert _client(session).get_media_url("M") == "https://lookaside.example/x"
    assert session.calls[0][1] == "https://graph.facebook.com/v21.0/M"


def test_download_media_uses_bearer_and_size_cap(monkeypatch, tmp_path):
    seen = {}

    def fake_download(url, path, max_bytes, timeout, max_seconds=None, headers=None):
        seen.update(url=url, path=path, max_bytes=max_bytes, headers=headers)

    monkeypatch.setattr("channel.whatsapp.whatsapp_api.download_to_file", fake_download)
    dest = str(tmp_path / "x.jpg")
    _client(FakeSession()).download_media("https://lookaside.example/x", dest, 1234)
    assert seen["headers"] == {"Authorization": "Bearer TOKEN"}
    assert seen["max_bytes"] == 1234 and seen["path"] == dest


def test_request_with_params_and_delete():
    session = FakeSession(FakeResponse(body={"success": True}))
    _client(session).request("DELETE", "GRP/participants", json={"participants": []},
                             params={"a": "b"})
    method, url, kw = session.calls[0]
    assert method == "DELETE" and url.endswith("/v21.0/GRP/participants")
    assert kw["params"] == {"a": "b"}


def test_empty_success_body_returns_empty_dict():
    session = FakeSession(FakeResponse(200, body=None, text=""))
    assert _client(session).request("DELETE", "GRP") == {}


def test_requires_token_and_phone_number_id():
    with pytest.raises(ValueError):
        WhatsAppApiClient("", "PNID")
    with pytest.raises(ValueError):
        WhatsAppApiClient("T", "")
