"""WhatsApp webhook HTTP layer: routes GET/POST /whatsapp/webhook to callbacks
bound to one channel instance, and never leaks a web.py debug page."""
import socket

import pytest

from channel.whatsapp.whatsapp_server import WEBHOOK_PATH, WebhookServer, build_wsgi_app


def _app(on_verify=None, on_event=None):
    return build_wsgi_app(on_verify or (lambda m, t, c: None), on_event or (lambda raw, sig: 200))


def test_get_echoes_challenge_from_callback():
    seen = {}

    def on_verify(mode, token, challenge):
        seen.update(mode=mode, token=token, challenge=challenge)
        return challenge

    resp = _app(on_verify=on_verify).request(
        WEBHOOK_PATH + "?hub.mode=subscribe&hub.verify_token=tok&hub.challenge=42")
    assert resp.status.startswith("200")
    assert resp.data == b"42"
    assert seen == {"mode": "subscribe", "token": "tok", "challenge": "42"}


def test_get_forbidden_when_callback_refuses():
    resp = _app().request(WEBHOOK_PATH + "?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=1")
    assert resp.status.startswith("403")


def test_post_passes_raw_body_and_signature():
    seen = {}

    def on_event(raw, sig):
        seen.update(raw=raw, sig=sig)
        return 200

    body = '{"a": 1}'
    resp = _app(on_event=on_event).request(
        WEBHOOK_PATH, method="POST", data=body, headers={"X-Hub-Signature-256": "sha256=ab"})
    assert resp.status.startswith("200")
    assert seen == {"raw": body.encode(), "sig": "sha256=ab"}


def test_post_status_comes_from_callback():
    resp = _app(on_event=lambda raw, sig: 403).request(WEBHOOK_PATH, method="POST", data="{}")
    assert resp.status.startswith("403")


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_callback_exception_is_a_plain_500_without_debug_page(method):
    def boom(*_a):
        raise RuntimeError("secret internals")

    app = build_wsgi_app(boom, boom)
    resp = app.request(WEBHOOK_PATH + "?hub.mode=subscribe", method=method, data="{}")
    assert resp.status.startswith("500")
    assert b"secret internals" not in resp.data
    assert b"Traceback" not in resp.data


def test_two_apps_are_bound_to_their_own_callbacks():
    a = _app(on_event=lambda raw, sig: 200)
    b = _app(on_event=lambda raw, sig: 403)
    assert a.request(WEBHOOK_PATH, method="POST", data="{}").status.startswith("200")
    assert b.request(WEBHOOK_PATH, method="POST", data="{}").status.startswith("403")


def test_other_paths_are_not_found():
    assert _app().request("/static/x").status.startswith("404")


def test_prepare_raises_when_port_is_taken():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        server = WebhookServer("127.0.0.1", port, lambda *a: None, lambda *a: 200)
        with pytest.raises(OSError):
            server.prepare()
    finally:
        holder.close()
