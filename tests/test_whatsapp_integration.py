"""End-to-end WhatsApp channel over real sockets.

A real WebhookServer (cheroot) receives a signed Meta webhook. The message runs
through ChatChannel's own produce/consume pipeline, and the reply leaves through
WhatsAppSender + WhatsAppApiClient to a local fake Graph API server. Only the
LLM (Channel.build_reply_content) and agent routing are stubbed.
"""
import functools
import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bridge.reply import Reply, ReplyType
from channel.channel import Channel
from channel.whatsapp import whatsapp_channel as wa_mod
from channel.whatsapp.whatsapp_api import WhatsAppApiClient
from channel.whatsapp.whatsapp_channel import WhatsAppChannel

SECRET = "integration-secret"
PNID = "10101"


class FakeGraph:
    """Records every Graph call; answers like Meta does."""

    def __init__(self):
        self.requests = []
        self.event = threading.Event()
        graph = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                graph.requests.append((self.command, self.path, body, dict(self.headers)))
                return body

            def do_POST(self):
                self._record()
                if self.path.endswith("/messages"):
                    self._reply({"messaging_product": "whatsapp", "messages": [{"id": "wamid.out"}]})
                    graph.event.set()
                elif self.path.endswith("/groups"):
                    self._reply({"id": "GROUP-1"})
                else:
                    self._reply({"success": True})

            def do_GET(self):
                self._record()
                if self.path.split("?")[0].endswith("/invite_link"):
                    self._reply({"messaging_product": "whatsapp",
                                 "invite_link": "https://chat.whatsapp.com/abc"})
                else:
                    self._reply({"id": "x"})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def messages(self):
        return [r for r in self.requests if r[1].endswith("/messages")]

    def close(self):
        self.server.shutdown()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def graph():
    g = FakeGraph()
    yield g
    g.close()


@pytest.fixture
def running(monkeypatch, graph):
    port = _free_port()
    settings = {
        "whatsapp_access_token": "TOKEN", "whatsapp_phone_number_id": PNID,
        "whatsapp_app_secret": SECRET, "whatsapp_verify_token": "vt", "whatsapp_port": port,
        "whatsapp_pair_interval_seconds": 0.05,
    }
    monkeypatch.setattr(wa_mod, "WhatsAppApiClient",
                        functools.partial(WhatsAppApiClient, base_url=graph.url))
    # Stub the LLM and agent routing; everything else is the real pipeline.
    monkeypatch.setattr(Channel, "build_reply_content",
                        lambda self, query, context=None: Reply(ReplyType.TEXT, f"echo: {query}"))

    def no_bridge(*_a, **_k):
        raise RuntimeError("no agent bridge in tests")

    from bridge.bridge import Bridge

    monkeypatch.setattr(Bridge.__wrapped__, "get_agent_bridge", no_bridge)

    ch = WhatsAppChannel.__wrapped__()
    ch.channel_type = "whatsapp"
    monkeypatch.setattr(ch, "cfg", lambda key, default=None: settings.get(key, default))
    thread = threading.Thread(target=ch.startup, daemon=True)
    thread.start()
    ok, err = ch.wait_startup(timeout=5)
    assert ok, err
    _wait_listening(port)
    ch.base = f"http://127.0.0.1:{port}/whatsapp/webhook"
    yield ch
    ch.stop()
    thread.join(timeout=5)


def _wait_listening(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("webhook server did not start listening")


def _post(url, body: bytes, signature=None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _webhook(text, msg_id, group_id=None):
    message = {"from": "15551234567", "id": msg_id, "timestamp": "1", "type": "text", "text": {"body": text}}
    if group_id:
        message["group_id"] = group_id
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "w", "changes": [{
        "field": "messages", "value": {
            "metadata": {"phone_number_id": PNID},
            "contacts": [{"profile": {"name": "Alice"}, "wa_id": "15551234567"}],
            "messages": [message],
        }}]}]}).encode()


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_verify_handshake_over_http(running):
    url = running.base + "?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=777"
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200 and resp.read() == b"777"
    try:
        urllib.request.urlopen(running.base + "?hub.mode=subscribe&hub.verify_token=no&hub.challenge=1",
                               timeout=5)
        raise AssertionError("expected 403")
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_signed_message_gets_a_reply_through_graph(running, graph):
    body = _webhook("hello bot", "wamid.in1")
    assert _post(running.base, body, _sign(body)) == 200
    assert _wait_for(lambda: graph.messages()), "no reply reached the Graph API"
    method, path, payload, headers = graph.messages()[0]
    assert path == f"/v21.0/{PNID}/messages"
    assert headers["Authorization"] == "Bearer TOKEN"
    assert payload["recipient_type"] == "individual"
    assert payload["to"] == "15551234567"
    assert payload["type"] == "text"
    assert "echo: hello bot" in payload["text"]["body"]


def test_group_message_reply_goes_to_group(running, graph):
    body = _webhook("hi team", "wamid.in2", group_id="GROUP-9")
    assert _post(running.base, body, _sign(body)) == 200
    assert _wait_for(lambda: graph.messages())
    payload = graph.messages()[0][2]
    assert payload["recipient_type"] == "group"
    assert payload["to"] == "GROUP-9"
    assert not payload["text"]["body"].startswith("@")


def test_bad_signature_rejected_and_nothing_sent(running, graph):
    body = _webhook("evil", "wamid.in3")
    assert _post(running.base, body, "sha256=" + "0" * 64) == 403
    assert _post(running.base, body) == 403
    time.sleep(0.5)
    assert graph.messages() == []


def test_redelivered_webhook_is_answered_once(running, graph):
    body = _webhook("once", "wamid.in4")
    assert _post(running.base, body, _sign(body)) == 200
    assert _post(running.base, body, _sign(body)) == 200
    assert _wait_for(lambda: graph.messages())
    time.sleep(0.5)
    assert len(graph.messages()) == 1


def test_oversized_body_is_refused_before_processing(running, graph):
    body = b"{" + b" " * (5 * 1024 * 1024) + b"}"
    status = None
    try:
        status = _post(running.base, body, _sign(body))
    except (ConnectionError, urllib.error.URLError):
        status = 413  # server may close the socket while we are still writing
    assert status == 413


def test_group_tool_against_graph(running, graph, monkeypatch):
    from agent.tools.whatsapp_group.whatsapp_group import WhatsAppGroupTool

    class Manager:
        def find_channels_by_type(self, channel_type):
            return [("whatsapp", running)]

    monkeypatch.setattr("common.channel_registry.get_channel_manager", lambda: Manager())
    tool = WhatsAppGroupTool()
    assert tool.is_available()

    created = tool.execute({"action": "create", "subject": "Launch crew"})
    assert created.status == "success"
    assert json.loads(created.result) == {"id": "GROUP-1"}
    link = tool.execute({"action": "get_invite_link", "group_id": "GROUP-1"})
    assert json.loads(link.result)["invite_link"].startswith("https://chat.whatsapp.com/")

    create_req = next(r for r in graph.requests if r[1].endswith("/groups"))
    assert create_req[0] == "POST" and create_req[2]["subject"] == "Launch crew"
