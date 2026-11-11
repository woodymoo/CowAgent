"""WhatsAppChannel: lifecycle, webhook handling, inbound mapping into the
ChatChannel pipeline, and send() dispatch, including the retry contract with
ChatChannel._send (raise only when nothing was delivered)."""
import hashlib
import hmac
import io
import json
import threading

import pytest

from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.file_cache import get_file_cache
from channel.whatsapp import whatsapp_channel as wa_mod
from channel.whatsapp.whatsapp_api import WhatsAppApiError
from channel.whatsapp.whatsapp_channel import WhatsAppChannel
from channel.whatsapp.whatsapp_rate_limiter import ThrottleTimeout
from channel.whatsapp.whatsapp_webhook import InboundMessage
from config import conf

SECRET = "app-secret"
PNID = "PNID"


class FakeSender:
    def __init__(self, fail_at=None, error=None):
        self.sent = []
        self.fail_at = fail_at
        self.error = error

    def send(self, to, recipient_type, msg_type, body):
        if self.fail_at is not None and len(self.sent) + 1 == self.fail_at:
            raise self.error
        self.sent.append((to, recipient_type, msg_type, body))
        return {"messages": [{"id": f"wamid.out{len(self.sent)}"}]}


class FakeClient:
    phone_number_id = PNID

    def __init__(self, tmp_path=None):
        self.uploads = []
        self.downloads = []
        self.tmp_path = tmp_path

    def upload_media(self, path, mime):
        with open(path, "rb") as f:
            self.uploads.append((path, mime, f.read()))
        return f"MEDIA{len(self.uploads)}"

    def get_media_url(self, media_id):
        return f"https://lookaside.example/{media_id}"

    def download_media(self, url, dest, max_bytes):
        self.downloads.append((url, dest, max_bytes))
        with open(dest, "wb") as f:
            f.write(b"media-bytes")


class InlineExecutor:
    def __init__(self):
        self.shutdown_called = False

    def submit(self, fn, *args, **kwargs):
        if self.shutdown_called:
            raise RuntimeError("cannot schedule new futures after shutdown")
        fn(*args, **kwargs)

    def shutdown(self, wait=False):
        self.shutdown_called = True


@pytest.fixture
def channel(monkeypatch, tmp_path):
    ch = WhatsAppChannel.__wrapped__()
    ch.channel_type = "whatsapp"
    ch.phone_number_id = PNID
    ch._app_secret = SECRET
    ch._verify_token = "verify-me"
    ch.client = FakeClient(tmp_path)
    ch.sender = FakeSender()
    ch._executor = InlineExecutor()
    produced = []
    monkeypatch.setattr(ch, "produce", produced.append)
    ch.produced = produced
    monkeypatch.setattr(wa_mod, "_tmp_path", lambda prefix, ext="": str(tmp_path / f"{prefix}{ext}"))
    yield ch
    for sid in ("1555", "GRP"):
        get_file_cache().clear(sid)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _webhook(messages, contacts=None):
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID},
            "contacts": contacts or [{"profile": {"name": "Alice"}, "wa_id": "1555"}],
            "messages": messages,
        }}]}],
    }).encode()


def _text(text="hello", msg_id="wamid.1", **extra):
    return dict({"from": "1555", "id": msg_id, "timestamp": "1", "type": "text", "text": {"body": text}},
                **extra)


# -- startup -------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["whatsapp_access_token", "whatsapp_phone_number_id",
                                     "whatsapp_app_secret", "whatsapp_verify_token"])
def test_startup_fails_fast_on_missing_setting(monkeypatch, missing):
    settings = {"whatsapp_access_token": "t", "whatsapp_phone_number_id": "p",
                "whatsapp_app_secret": "s", "whatsapp_verify_token": "v"}
    settings[missing] = ""
    ch = WhatsAppChannel.__wrapped__()
    monkeypatch.setattr(ch, "cfg", lambda key, default=None: settings.get(key, default))
    ch.startup()
    ok, err = ch.wait_startup(timeout=0.1)
    assert not ok and missing in err


def test_startup_reports_bind_failure(monkeypatch):
    settings = {"whatsapp_access_token": "t", "whatsapp_phone_number_id": "p",
                "whatsapp_app_secret": "s", "whatsapp_verify_token": "v", "whatsapp_port": 1}

    class FailingServer:
        def __init__(self, *a, **k):
            pass

        def prepare(self):
            raise OSError("address already in use")

    ch = WhatsAppChannel.__wrapped__()
    monkeypatch.setattr(ch, "cfg", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(wa_mod, "WebhookServer", FailingServer)
    ch.startup()
    ok, err = ch.wait_startup(timeout=0.1)
    assert not ok and "address already in use" in err


def test_startup_reports_success_only_after_bind(monkeypatch):
    settings = {"whatsapp_access_token": "t", "whatsapp_phone_number_id": "p",
                "whatsapp_app_secret": "s", "whatsapp_verify_token": "v"}
    order = []

    class Server:
        def __init__(self, *a, **k):
            pass

        def prepare(self):
            order.append("prepare")

        def serve(self):
            order.append(("serve", ch._startup_event.is_set()))

    ch = WhatsAppChannel.__wrapped__()
    monkeypatch.setattr(ch, "cfg", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(wa_mod, "WebhookServer", Server)
    ch.startup()
    assert order == ["prepare", ("serve", True)]
    assert ch.wait_startup(0.1) == (True, "")
    assert ch.group_service is not None


# -- webhook -------------------------------------------------------------------

def test_verify_uses_configured_token(channel):
    assert channel._on_verify("subscribe", "verify-me", "c1") == "c1"
    assert channel._on_verify("subscribe", "nope", "c1") is None


def test_bad_signature_is_403_and_not_processed(channel):
    body = _webhook([_text()])
    assert channel._on_event(body, "sha256=00") == 403
    assert channel._on_event(body, None) == 403
    assert channel.produced == []


@pytest.mark.parametrize("body", [b"not json", b'{"object": "page"}'])
def test_signed_but_bad_payload_is_acked_to_stop_meta_retries(channel, body):
    assert channel._on_event(body, _sign(body)) == 200
    assert channel.produced == []


def test_event_after_shutdown_asks_meta_to_retry_later(channel):
    channel._executor.shutdown()
    body = _webhook([_text()])
    assert channel._on_event(body, _sign(body)) == 500


def test_text_one_to_one_reaches_produce(channel):
    body = _webhook([_text("hello")])
    assert channel._on_event(body, _sign(body)) == 200
    (ctx,) = channel.produced
    assert ctx.type == ContextType.TEXT
    assert ctx.content == "hello"
    assert ctx["session_id"] == "1555"
    assert ctx["receiver"] == "1555"
    assert ctx["channel_type"] == "whatsapp"
    assert ctx["msg"].from_user_nickname == "Alice"
    assert not ctx.get("isgroup")


def test_duplicate_delivery_is_processed_once(channel):
    body = _webhook([_text()])
    channel._on_event(body, _sign(body))
    channel._on_event(body, _sign(body))
    assert len(channel.produced) == 1


def test_dedup_is_atomic_across_threads(channel):
    inbound = InboundMessage(msg_id="wamid.race", sender_id="1555", msg_type="text", text="x")
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        channel._handle_inbound(inbound)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(channel.produced) == 1


def test_group_message_routes_to_group(channel, monkeypatch):
    monkeypatch.setitem(conf(), "whatsapp_group_trigger", "all")
    body = _webhook([_text("hey", group_id="GRP")])
    channel._on_event(body, _sign(body))
    (ctx,) = channel.produced
    assert ctx.get("isgroup") is True
    assert ctx["receiver"] == "group:GRP"
    assert ctx["session_id"] == "GRP"
    assert ctx["no_need_at"] is True
    assert ctx["msg"].actual_user_id == "1555"


def test_group_session_per_user_when_not_shared(channel, monkeypatch):
    monkeypatch.setitem(conf(), "whatsapp_group_trigger", "all")
    monkeypatch.setitem(conf(), "group_shared_session", False)
    body = _webhook([_text("hey", group_id="GRP")])
    channel._on_event(body, _sign(body))
    assert channel.produced[0]["session_id"] == "1555:GRP"


def test_group_prefix_trigger(channel, monkeypatch):
    monkeypatch.setitem(conf(), "whatsapp_group_trigger", "prefix")
    monkeypatch.setitem(conf(), "group_chat_prefix", ["@bot"])
    body = _webhook([_text("no prefix", msg_id="a", group_id="GRP"),
                     _text("@bot do it", msg_id="b", group_id="GRP")])
    channel._on_event(body, _sign(body))
    (ctx,) = channel.produced
    assert ctx.content == "do it"


def test_one_to_one_ignores_legacy_single_chat_prefix(channel, monkeypatch):
    """No global conf mutation: the channel itself skips the legacy WeChat prefix gate."""
    monkeypatch.setitem(conf(), "single_chat_prefix", ["bot"])
    body = _webhook([_text("plain question")])
    channel._on_event(body, _sign(body))
    assert channel.produced[0].content == "plain question"
    assert conf()["single_chat_prefix"] == ["bot"]


def test_standalone_image_is_cached_then_attached_to_next_text(channel):
    img = {"from": "1555", "id": "wamid.i", "timestamp": "1", "type": "image",
           "image": {"id": "MID", "mime_type": "image/jpeg"}}
    body = _webhook([img])
    channel._on_event(body, _sign(body))
    assert channel.produced == []
    assert channel.client.downloads[0][0] == "https://lookaside.example/MID"
    body = _webhook([_text("what is this", msg_id="wamid.t")])
    channel._on_event(body, _sign(body))
    (ctx,) = channel.produced
    assert ctx.content.startswith("what is this")
    assert "[image: " in ctx.content


def test_image_with_caption_is_one_query(channel):
    img = {"from": "1555", "id": "wamid.c", "timestamp": "1", "type": "image",
           "image": {"id": "MID", "mime_type": "image/jpeg", "caption": "describe"}}
    body = _webhook([img])
    channel._on_event(body, _sign(body))
    (ctx,) = channel.produced
    assert ctx.type == ContextType.TEXT
    assert ctx.content.startswith("describe\n[image: ")


def test_document_keeps_safe_filename(channel):
    doc = {"from": "1555", "id": "wamid.d", "timestamp": "1", "type": "document",
           "document": {"id": "MID", "mime_type": "application/pdf", "filename": "../../etc/r.pdf",
                        "caption": "summarize"}}
    body = _webhook([doc])
    channel._on_event(body, _sign(body))
    dest = channel.client.downloads[0][1]
    assert ".." not in dest.replace(str(channel.client.tmp_path), "")
    assert dest.endswith("r.pdf")
    assert "[file: " in channel.produced[0].content


def test_audio_becomes_voice_context(channel):
    audio = {"from": "1555", "id": "wamid.v", "timestamp": "1", "type": "audio",
             "audio": {"id": "MID", "mime_type": "audio/ogg; codecs=opus"}}
    body = _webhook([audio])
    channel._on_event(body, _sign(body))
    (ctx,) = channel.produced
    assert ctx.type == ContextType.VOICE
    assert ctx.content.endswith(".ogg")


def test_media_download_failure_is_logged_and_skipped(channel, monkeypatch):
    def boom(*a):
        raise WhatsAppApiError(404, message="gone")

    monkeypatch.setattr(channel.client, "get_media_url", boom)
    img = {"from": "1555", "id": "wamid.x", "timestamp": "1", "type": "image",
           "image": {"id": "MID", "mime_type": "image/jpeg", "caption": "c"}}
    body = _webhook([img])
    assert channel._on_event(body, _sign(body)) == 200
    assert channel.produced == []


def test_unsupported_type_is_skipped(channel):
    sticker = {"from": "1555", "id": "wamid.s", "timestamp": "1", "type": "sticker",
               "sticker": {"id": "S"}}
    body = _webhook([sticker])
    channel._on_event(body, _sign(body))
    assert channel.produced == []


# -- send ----------------------------------------------------------------------

def _ctx(receiver="1555"):
    return {"receiver": receiver}


class Ctx(dict):
    """Minimal stand-in for bridge Context: send() only reads keys."""


def test_send_text_individual(channel):
    channel.send(Reply(ReplyType.TEXT, "hi"), Ctx(_ctx()))
    assert channel.sender.sent == [("1555", "individual", "text", {"body": "hi"})]


def test_send_text_group(channel):
    channel.send(Reply(ReplyType.TEXT, "hi"), Ctx(_ctx("group:GRP")))
    assert channel.sender.sent[0][:3] == ("GRP", "group", "text")


def test_send_long_text_is_split_under_limit(channel):
    channel.send(Reply(ReplyType.TEXT, "line\n" * 2000), Ctx(_ctx()))
    assert len(channel.sender.sent) > 1
    assert all(len(s[3]["body"]) <= 4096 for s in channel.sender.sent)


def test_send_error_and_info_are_text(channel):
    channel.send(Reply(ReplyType.ERROR, "bad"), Ctx(_ctx()))
    assert channel.sender.sent[0][2] == "text"


def test_retryable_failure_before_any_delivery_raises_for_chat_channel_retry(channel):
    channel.sender = FakeSender(fail_at=1, error=WhatsAppApiError(503))
    with pytest.raises(WhatsAppApiError):
        channel.send(Reply(ReplyType.TEXT, "hi"), Ctx(_ctx()))


def test_failure_after_partial_delivery_does_not_raise(channel):
    channel.sender = FakeSender(fail_at=2, error=WhatsAppApiError(503))
    channel.send(Reply(ReplyType.TEXT, "line\n" * 2000), Ctx(_ctx()))
    assert len(channel.sender.sent) == 1


@pytest.mark.parametrize("error", [WhatsAppApiError(400, code=131047), WhatsAppApiError(400, code=131048),
                                   ThrottleTimeout("too long")])
def test_non_retryable_failures_do_not_raise(channel, error):
    channel.sender = FakeSender(fail_at=1, error=error)
    channel.send(Reply(ReplyType.TEXT, "hi"), Ctx(_ctx()))  # must not raise


def test_send_local_png_as_image(channel, tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
    channel.send(Reply(ReplyType.IMAGE_URL, "file://" + str(f)), Ctx(_ctx()))
    assert channel.client.uploads[0][1] == "image/png"
    assert channel.sender.sent == [("1555", "individual", "image", {"id": "MEDIA1"})]


def test_send_gif_is_not_uploaded_user_gets_notice(channel, tmp_path):
    """Meta accepts only jpeg/png images and a fixed document list; a GIF would be
    rejected with a non-retryable 400 and the user would get nothing at all."""
    f = tmp_path / "anim.gif"
    f.write_bytes(b"GIF89a")
    channel.send(Reply(ReplyType.IMAGE_URL, "file://" + str(f)), Ctx(_ctx()))
    assert channel.client.uploads == []
    (sent,) = channel.sender.sent
    assert sent[2] == "text" and "anim.gif" in sent[3]["body"]


def test_send_pdf_and_office_files_as_document(channel, tmp_path):
    for name in ("a.pdf", "b.docx", "c.xlsx"):
        (tmp_path / name).write_bytes(b"x")
        channel.send(Reply(ReplyType.FILE, str(tmp_path / name)), Ctx(_ctx()))
    assert [s[2] for s in channel.sender.sent] == ["document"] * 3


def test_send_text_like_file_as_plain_text_document(channel, tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# hi")
    channel.send(Reply(ReplyType.FILE, str(f)), Ctx(_ctx()))
    assert channel.client.uploads[0][1] == "text/plain"
    _, _, mtype, body = channel.sender.sent[0]
    assert mtype == "document" and body["filename"] == "notes.md"


def test_send_unsupported_binary_file_sends_notice(channel, tmp_path):
    f = tmp_path / "bundle.zip"
    f.write_bytes(b"PK")
    channel.send(Reply(ReplyType.FILE, str(f)), Ctx(_ctx()))
    assert channel.client.uploads == []
    assert channel.sender.sent[0][2] == "text" and "bundle.zip" in channel.sender.sent[0][3]["body"]


def test_send_image_bytesio(channel):
    channel.send(Reply(ReplyType.IMAGE, io.BytesIO(b"\xff\xd8\xff\xe0jpeg")), Ctx(_ctx()))
    assert channel.client.uploads[0][1] == "image/jpeg"
    assert channel.sender.sent[0][2] == "image"


def test_send_remote_image_uses_guarded_download(channel, monkeypatch):
    seen = {}

    def fake_download(url, path, max_bytes=None, timeout=None, max_seconds=None, guarded=False, **kw):
        seen.update(url=url, guarded=guarded, max_seconds=max_seconds)
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xffjpeg")

    monkeypatch.setattr(wa_mod, "download_to_file", fake_download)
    channel.send(Reply(ReplyType.IMAGE_URL, "https://example.com/a.jpg"), Ctx(_ctx()))
    assert seen["guarded"] is True and seen["max_seconds"]
    assert channel.sender.sent[0][2] == "image"


def test_send_remote_download_failure_is_logged_not_raised(channel, monkeypatch):
    def boom(*a, **k):
        raise OSError("blocked")

    monkeypatch.setattr(wa_mod, "download_to_file", boom)
    channel.send(Reply(ReplyType.IMAGE_URL, "https://example.com/a.jpg"), Ctx(_ctx()))
    assert channel.sender.sent == []


def test_send_file_with_caption(channel, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    reply = Reply(ReplyType.FILE, str(f))
    reply.text_content = "here you go"
    channel.send(reply, Ctx(_ctx()))
    _, _, mtype, body = channel.sender.sent[0]
    assert mtype == "document"
    assert body == {"id": "MEDIA1", "filename": "report.pdf", "caption": "here you go"}


def test_send_file_with_overlong_caption_sends_text_after(channel, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    reply = Reply(ReplyType.FILE, str(f))
    reply.text_content = "x" * 2000
    channel.send(reply, Ctx(_ctx()))
    assert [s[2] for s in channel.sender.sent] == ["document", "text"]
    assert "caption" not in channel.sender.sent[0][3]


def test_send_video_and_voice(channel, tmp_path):
    v = tmp_path / "clip.mp4"
    v.write_bytes(b"mp4")
    a = tmp_path / "speech.mp3"
    a.write_bytes(b"mp3")
    channel.send(Reply(ReplyType.VIDEO, "file://" + str(v)), Ctx(_ctx()))
    channel.send(Reply(ReplyType.VOICE, str(a)), Ctx(_ctx()))
    assert [s[2] for s in channel.sender.sent] == ["video", "audio"]


def test_send_unsupported_audio_format_sends_notice(channel, tmp_path):
    a = tmp_path / "speech.wav"
    a.write_bytes(b"RIFF")
    channel.send(Reply(ReplyType.VOICE, str(a)), Ctx(_ctx()))
    assert channel.client.uploads == []
    assert channel.sender.sent[0][2] == "text"


def test_remote_download_cap_follows_media_kind(channel, monkeypatch):
    """Meta caps images at 5 MB: don't download 100 MB just to be refused."""
    seen = {}

    def fake_download(url, path, max_bytes=None, **kw):
        seen["max_bytes"] = max_bytes
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xffjpeg")

    monkeypatch.setattr(wa_mod, "download_to_file", fake_download)
    channel.send(Reply(ReplyType.IMAGE_URL, "https://example.com/a.jpg"), Ctx(_ctx()))
    assert seen["max_bytes"] == 5 * 1024 * 1024


def test_group_event_log_omits_invite_link(channel, caplog):
    from channel.whatsapp.whatsapp_webhook import GroupEvent, WebhookBatch
    import logging
    caplog.set_level(logging.DEBUG)
    event = GroupEvent("group_lifecycle_update",
                       {"groups": [{"group_id": "G1", "invite_link": "https://chat.whatsapp.com/SECRET"}]})
    channel._process_batch(WebhookBatch(group_events=(event,)))
    assert "SECRET" not in caplog.text


def test_send_without_receiver_is_dropped(channel):
    channel.send(Reply(ReplyType.TEXT, "hi"), Ctx({}))
    assert channel.sender.sent == []


def test_send_before_startup_is_dropped():
    ch = WhatsAppChannel.__wrapped__()
    ch.send(Reply(ReplyType.TEXT, "hi"), Ctx(_ctx()))  # must not raise


def test_stop_shuts_down_server_and_executor(channel):
    stopped = []
    channel._server = type("S", (), {"stop": lambda self: stopped.append(True)})()
    channel.stop()
    assert stopped == [True]
    assert channel._executor.shutdown_called


# -- review fixes ----------------------------------------------------------------

class DeferredExecutor:
    """Runs queued tasks later, newest first: the worst-case interleaving of a
    thread pool, where a later webhook overtakes an earlier one."""

    def __init__(self):
        self.tasks = []

    def submit(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))

    def run_lifo(self):
        while self.tasks:
            fn, args, kwargs = self.tasks.pop()
            fn(*args, **kwargs)

    def shutdown(self, wait=False):
        pass


def _image(msg_id="wamid.img"):
    return {"from": "1555", "id": msg_id, "timestamp": "1", "type": "image",
            "image": {"id": "IMG", "mime_type": "image/jpeg"}}


def test_messages_from_one_sender_keep_their_order(channel):
    """Photo without caption, then "describe it" in a second webhook: the text
    must not overtake the photo's download, or it is answered without the photo."""
    executor = DeferredExecutor()
    channel._executor = executor
    for body in (_webhook([_image()]), _webhook([_text("describe it", msg_id="wamid.2")])):
        assert channel._on_event(body, _sign(body)) == 200
    executor.run_lifo()
    (ctx,) = channel.produced
    assert ctx.content.startswith("describe it") and "[image:" in ctx.content


def test_different_senders_are_processed_independently(channel):
    executor = DeferredExecutor()
    channel._executor = executor
    other = _text("hi", msg_id="wamid.b")
    other["from"] = "1666"
    for body in (_webhook([_text("hello")]), _webhook([other], contacts=[{"wa_id": "1666"}])):
        channel._on_event(body, _sign(body))
    assert len(executor.tasks) == 2  # one serial drain per sender, not one global queue
    executor.run_lifo()
    assert sorted(c.content for c in channel.produced) == ["hello", "hi"]


def test_inbox_is_released_after_draining(channel):
    body = _webhook([_text("hello")])
    channel._on_event(body, _sign(body))
    assert channel._inbox == {}


def test_oversized_local_image_is_not_uploaded_user_gets_notice(channel, tmp_path):
    """Meta caps images at 5 MB and answers a bigger upload with a non-retryable
    400, so the user would get nothing at all."""
    f = tmp_path / "big.png"
    with open(f, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.truncate(6 * 1024 * 1024)
    channel.send(Reply(ReplyType.IMAGE_URL, "file://" + str(f)), Ctx(_ctx()))
    assert channel.client.uploads == []
    (sent,) = channel.sender.sent
    assert sent[2] == "text" and "big.png" in sent[3]["body"]
