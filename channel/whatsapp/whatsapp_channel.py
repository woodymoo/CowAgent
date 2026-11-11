"""WhatsApp channel via the official Meta WhatsApp Business Cloud API.

The Agent acts as a WhatsApp *business* number (with its approved Business
profile). Integration is HTTP only: Meta pushes inbound events to our webhook,
and we call the Graph API to reply. There is no browser automation and no
WhatsApp Web protocol.

Flow:
    Meta ─POST /whatsapp/webhook─> WebhookServer ─> _on_event (signature, schema)
        ─> per-chat inbox (FIFO) ─> executor ─> _handle_inbound ─> _compose_context
        ─> ChatChannel.produce
    ChatChannel ─> send() ─> WhatsAppSender (throttle + retry) ─> Graph API

Single-instance by design, like the other webhook-port channels: the listening
port is global config, not a per-instance credential.
"""

import io
import json
import mimetypes
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import safe_filename
from channel.file_cache import get_file_cache
from channel.whatsapp.whatsapp_api import WhatsAppApiClient, WhatsAppApiError
from channel.whatsapp.whatsapp_groups import WhatsAppGroupService
from channel.whatsapp.whatsapp_message import WhatsAppMessage
from channel.whatsapp.whatsapp_rate_limiter import SendThrottle, ThrottleTimeout
from channel.whatsapp.whatsapp_sender import WhatsAppSender
from channel.whatsapp.whatsapp_server import WebhookServer
from channel.whatsapp.whatsapp_webhook import (
    InboundMessage,
    WebhookBatch,
    WebhookSchemaError,
    parse_webhook,
    verify_signature,
    verify_subscription,
)
from common import state_dir
from common.expired_dict import ExpiredDict
from common.i18n import t as _t
from common.log import logger
from common.markdown_fence import split_markdown
from common.media_download import download_to_file, remove_download
from common.singleton import singleton
from config import conf

REQUIRED_SETTINGS = (
    "whatsapp_access_token",
    "whatsapp_phone_number_id",
    "whatsapp_app_secret",
    "whatsapp_verify_token",
)
GROUP_RECEIVER_PREFIX = "group:"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
_MAX_INBOUND_MEDIA_BYTES = 100 * 1024 * 1024
_MAX_REMOTE_MEDIA_SECONDS = 300
# Media types and size limits Meta's /media endpoint accepts. Anything else is
# refused with a non-retryable 400, so it is never uploaded: the user gets a short
# notice instead of silence.
_IMAGE_MIMES = ("image/jpeg", "image/png")
_VIDEO_MIMES = ("video/mp4", "video/3gpp")
_AUDIO_MIMES = ("audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg")
_DOCUMENT_MIMES = (
    "text/plain", "application/pdf",
    "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
)
_TEXT_LIKE_MIMES = ("application/json", "application/xml", "application/x-yaml", "application/yaml")
_TEXT_LIKE_EXTS = (".md", ".markdown", ".csv", ".log", ".yaml", ".yml", ".json", ".txt", ".xml")
_ALLOWED_MIMES = {"image": _IMAGE_MIMES, "video": _VIDEO_MIMES, "audio": _AUDIO_MIMES,
                  "document": _DOCUMENT_MIMES}
_MAX_MEDIA_BYTES = {"image": 5 * 1024 * 1024, "audio": 16 * 1024 * 1024,
                    "video": 16 * 1024 * 1024, "document": 100 * 1024 * 1024}
_REPLY_KINDS = {ReplyType.IMAGE: "image", ReplyType.IMAGE_URL: "image", ReplyType.VIDEO: "video",
                ReplyType.VIDEO_URL: "video", ReplyType.VOICE: "audio", ReplyType.FILE: "document"}
_INBOUND_CTYPES = {
    "text": ContextType.TEXT,
    "image": ContextType.IMAGE,
    "audio": ContextType.VOICE,
    "document": ContextType.FILE,
    "video": ContextType.FILE,
}


def _tmp_path(prefix: str, ext: str = "") -> str:
    return state_dir.tmp_file(prefix, ext)


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return "application/octet-stream"


@singleton
class WhatsAppChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()
        self.phone_number_id = ""
        self._app_secret = ""
        self._verify_token = ""
        self.client = None
        self.sender = None
        self.group_service = None
        self._server = None
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="whatsapp-webhook")
        # Per-chat FIFO of inbound messages. One drain task per chat at a time keeps
        # a chat's messages in arrival order (a text must not overtake the photo
        # sent before it) while different chats still run in parallel.
        self._inbox = {}
        self._inbox_lock = threading.Lock()
        # Meta re-delivers webhooks it thinks were not acknowledged.
        self._seen = ExpiredDict(60 * 60)
        self._seen_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self):
        missing = [key for key in REQUIRED_SETTINGS if not self.cfg(key, "")]
        if missing:
            self._startup_failed(f"[WhatsApp] missing required config: {', '.join(missing)}")
            return
        try:
            self._init_runtime()
            port = int(self.cfg("whatsapp_port", 9893))
            self._server = WebhookServer("0.0.0.0", port, self._on_verify, self._on_event)
            self._server.prepare()  # binds; raises if the port is taken
        except Exception as e:
            self._startup_failed(f"[WhatsApp] startup failed: {e}")
            return
        logger.info(f"[WhatsApp] ✅ webhook listening on http://0.0.0.0:{port}/whatsapp/webhook "
                    f"(phone_number_id={self.phone_number_id})")
        self.report_startup_success()
        self._server.serve()

    def _startup_failed(self, err: str):
        logger.error(err)
        self.report_startup_error(err)

    def _init_runtime(self):
        self.phone_number_id = str(self.cfg("whatsapp_phone_number_id"))
        self._app_secret = self.cfg("whatsapp_app_secret")
        self._verify_token = self.cfg("whatsapp_verify_token")
        self.client = WhatsAppApiClient(
            self.cfg("whatsapp_access_token"),
            self.phone_number_id,
            api_version=self.cfg("whatsapp_api_version", "v21.0"),
        )
        throttle = SendThrottle(
            global_rate=float(self.cfg("whatsapp_rate_limit_mps", 20)),
            pair_interval=float(self.cfg("whatsapp_pair_interval_seconds", 6)),
            pair_burst=int(self.cfg("whatsapp_pair_burst", 5)),
            max_wait=float(self.cfg("whatsapp_max_wait_seconds", 120)),
        )
        self.sender = WhatsAppSender(self.client, throttle)
        self.group_service = WhatsAppGroupService(self.client)

    def stop(self):
        logger.info("[WhatsApp] stop() called")
        if self._server is not None:
            try:
                self._server.stop()
            except Exception as e:
                logger.warning(f"[WhatsApp] error stopping webhook server: {e}")
            self._server = None
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Webhook callbacks (run on the HTTP server's threads: keep them fast)
    # ------------------------------------------------------------------

    def _on_verify(self, mode, token, challenge):
        result = verify_subscription(mode, token, challenge, self._verify_token)
        if result is None:
            logger.warning("[WhatsApp] webhook verification refused (mode or verify token mismatch)")
        return result

    def _on_event(self, raw: bytes, signature) -> int:
        if not verify_signature(self._app_secret, raw, signature):
            logger.warning("[WhatsApp] webhook rejected: bad or missing X-Hub-Signature-256")
            return 403
        try:
            batch = parse_webhook(json.loads(raw.decode("utf-8")), self.phone_number_id)
        except (ValueError, WebhookSchemaError) as e:
            # Signed by Meta but unusable: ack it, or Meta keeps retrying for days.
            logger.error(f"[WhatsApp] signed webhook payload rejected: {e}")
            return 200
        try:
            self._dispatch(batch)
        except RuntimeError as e:
            # Shutting down: a non-2xx makes Meta re-deliver after restart
            # (already-handled messages are dropped by the dedup).
            logger.warning(f"[WhatsApp] webhook not dispatched ({e}); asking Meta to retry")
            return 500
        return 200

    def _dispatch(self, batch: WebhookBatch):
        """Queue the batch in arrival order. Runs on the HTTP thread, so it only enqueues."""
        if batch.statuses or batch.group_events:
            self._executor.submit(self._process_batch, batch)
        for message in batch.messages:
            self._enqueue_inbound(message)

    def _enqueue_inbound(self, message: InboundMessage):
        key = message.group_id or message.sender_id
        with self._inbox_lock:
            queue = self._inbox.get(key)
            if queue is not None:
                queue.append(message)  # the running drain task will pick it up
                return
            self._inbox[key] = deque([message])
        try:
            self._executor.submit(self._drain_inbox, key)
        except RuntimeError:
            with self._inbox_lock:
                self._inbox.pop(key, None)
            raise

    def _drain_inbox(self, key: str):
        while True:
            with self._inbox_lock:
                queue = self._inbox[key]
                if not queue:
                    del self._inbox[key]
                    return
                message = queue.popleft()
            try:
                self._handle_inbound(message)
            except Exception as e:
                logger.error(f"[WhatsApp] inbound message {message.msg_id} failed: {e}", exc_info=True)

    def _process_batch(self, batch: WebhookBatch):
        """Log delivery statuses and group events. Inbound messages go through the inbox."""
        for status in batch.statuses:
            if status.status == "failed":
                logger.warning(f"[WhatsApp] delivery failed: msg={status.msg_id} to={status.recipient_id} "
                               f"errors={list(status.errors)}")
            else:
                logger.debug(f"[WhatsApp] status {status.status}: msg={status.msg_id}")
        for event in batch.group_events:
            # Payloads can carry invite links (anyone holding one can join), so log ids only.
            groups = event.value.get("groups") if isinstance(event.value.get("groups"), list) else []
            ids = [g.get("group_id") for g in groups if isinstance(g, dict)]
            logger.info(f"[WhatsApp] group event {event.field}: groups={ids}")

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def _first_delivery(self, msg_id: str) -> bool:
        with self._seen_lock:
            if self._seen.get(msg_id):
                return False
            self._seen[msg_id] = True
            return True

    def _handle_inbound(self, inbound: InboundMessage):
        if not self._first_delivery(inbound.msg_id):
            logger.debug(f"[WhatsApp] duplicate delivery skipped: {inbound.msg_id}")
            return
        ctype = _INBOUND_CTYPES.get(inbound.msg_type)
        if ctype is None:
            logger.debug(f"[WhatsApp] unsupported message type {inbound.msg_type!r} skipped")
            return

        text = inbound.text if ctype == ContextType.TEXT else inbound.caption
        if inbound.group_id:
            text = self._group_trigger(text)
            if text is None:
                logger.debug(f"[WhatsApp] group message not triggered: group={inbound.group_id}")
                return

        content = text
        if ctype != ContextType.TEXT:
            content = self._download_inbound_media(inbound)
            if content is None:
                return

        session_id = self._session_id(inbound)
        ctype, content = self._merge_with_file_cache(session_id, ctype, content, text)
        if ctype is None:
            return

        msg = WhatsAppMessage(inbound, ctype, content, self_id=self.phone_number_id)
        context = self._compose_context(ctype, content, isgroup=msg.is_group, msg=msg)
        if context is None:
            return
        from agent.team_addressing import stamp_speaker_from_channel
        stamp_speaker_from_channel(self, context, content)
        self.produce(context)

    def _group_trigger(self, text: str):
        """The query text to answer in a group, or ``None`` when the bot isn't addressed."""
        mode = conf().get("whatsapp_group_trigger", "all")
        if mode != "prefix":
            return text
        prefix = check_prefix(text or "", conf().get("group_chat_prefix") or [])
        if not prefix:
            return None
        return (text or "").replace(prefix, "", 1).strip()

    @staticmethod
    def _session_id(inbound: InboundMessage) -> str:
        if not inbound.group_id:
            return inbound.sender_id
        if conf().get("group_shared_session", True):
            return inbound.group_id
        return f"{inbound.sender_id}:{inbound.group_id}"

    def _merge_with_file_cache(self, session_id, ctype, content, text):
        """Same convention as Telegram: standalone media waits for the next text query."""
        file_cache = get_file_cache()
        if ctype in (ContextType.IMAGE, ContextType.FILE):
            tag = "image" if ctype == ContextType.IMAGE else "file"
            if text:
                return ContextType.TEXT, f"{text}\n[{tag}: {content}]"
            file_cache.add(session_id, content, file_type=tag)
            logger.info(f"[WhatsApp] {tag} cached for session {session_id}, waiting for query")
            return None, None
        if ctype == ContextType.TEXT:
            cached = file_cache.get(session_id)
            if cached:
                refs = [f"[{f['type'] if f['type'] in ('image', 'video') else 'file'}: {f['path']}]"
                        for f in cached]
                content = (content or "") + "\n" + "\n".join(refs)
                file_cache.clear(session_id)
        return ctype, content

    def _download_inbound_media(self, inbound: InboundMessage):
        name = safe_filename(inbound.filename)
        ext = os.path.splitext(name)[1] if name else ""
        if not ext:
            mime = inbound.mime_type.split(";")[0].strip()
            ext = ".ogg" if mime == "audio/ogg" else (mimetypes.guess_extension(mime) or "")
        path = _tmp_path(f"whatsapp_{safe_filename(inbound.media_id) or 'media'}", "_" + name if name else ext)
        try:
            url = self.client.get_media_url(inbound.media_id)
            self.client.download_media(url, path, _MAX_INBOUND_MEDIA_BYTES)
        except Exception as e:
            logger.error(f"[WhatsApp] media download failed: msg={inbound.msg_id} "
                         f"media={inbound.media_id}: {type(e).__name__}: {e}")
            remove_download(path)
            return None
        return path

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """Skip ChatChannel's legacy WeChat gating (whitelist, prefix, @-name).

        Triggering is decided in ``_handle_inbound``. Overriding here, as
        Telegram does, avoids mutating the process-wide ``conf()`` that other
        channels share.
        """
        context = Context(ctype, content)
        context.kwargs = kwargs
        context["channel_type"] = self.channel_type
        self.stamp_instance_context(context)
        context["origin_ctype"] = ctype
        msg = context["msg"]
        context["session_id"] = self._session_id(msg._rawmsg)
        if msg.is_group:
            context["receiver"] = GROUP_RECEIVER_PREFIX + msg.other_user_id
            # A literal "@name" is not a WhatsApp mention; don't prepend one.
            context["no_need_at"] = True
        else:
            context["receiver"] = msg.other_user_id

        if ctype == ContextType.TEXT:
            img_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_prefix:
                content = content.replace(img_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            context.content = (content or "").strip()
            if "desire_rtype" not in context and conf().get("always_reply_voice"):
                context["desire_rtype"] = ReplyType.VOICE
        elif ctype == ContextType.VOICE:
            if "desire_rtype" not in context and (conf().get("voice_reply_voice")
                                                  or conf().get("always_reply_voice")):
                context["desire_rtype"] = ReplyType.VOICE
        return context

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_recipient(receiver: str):
        receiver = str(receiver or "").strip()
        if receiver.startswith(GROUP_RECEIVER_PREFIX):
            return receiver[len(GROUP_RECEIVER_PREFIX):], "group"
        return receiver.lstrip("+"), "individual"

    def send(self, reply: Reply, context):
        """Deliver *reply*. Raises only when nothing went out and the error is
        transient, so ChatChannel._send's whole-reply retry can never duplicate
        a message or hammer a non-retryable error (131047/131048)."""
        if self.sender is None:
            logger.warning("[WhatsApp] channel not started, reply dropped")
            return
        to, recipient_type = self._resolve_recipient(context.get("receiver"))
        if not to:
            logger.warning("[WhatsApp] no receiver in context, reply dropped")
            return

        cleanup = []
        try:
            units = self._build_units(reply, cleanup)
            self._deliver(to, recipient_type, units)
        except (OSError, ValueError) as e:
            logger.error(f"[WhatsApp] cannot prepare {reply.type} reply for {to}: {type(e).__name__}: {e}")
        finally:
            for path in cleanup:
                remove_download(path)

    def _deliver(self, to, recipient_type, units):
        delivered = 0
        for msg_type, body_factory in units:
            try:
                self.sender.send(to, recipient_type, msg_type, body_factory())
                delivered += 1
            except WhatsAppApiError as e:
                if e.retryable and delivered == 0:
                    raise
                logger.error(f"[WhatsApp] send to {to} failed after {delivered} part(s): {e}")
                return
            except ThrottleTimeout as e:
                logger.error(f"[WhatsApp] send to {to} abandoned after {delivered} part(s): {e}")
                return
        logger.info(f"[WhatsApp] sent {delivered} part(s) to {recipient_type} {to}")

    def _build_units(self, reply: Reply, cleanup: list) -> list:
        """List of ``(msg_type, body_factory)``. Media is uploaded lazily, just before its turn."""
        rtype = reply.type
        if rtype in (ReplyType.TEXT, ReplyType.ERROR, ReplyType.INFO):
            return self._text_units(str(reply.content or ""))
        kind = _REPLY_KINDS.get(rtype)
        if kind is None:
            logger.warning(f"[WhatsApp] unsupported reply type {rtype}, dropped")
            return []
        if rtype == ReplyType.IMAGE:
            path = self._bytes_to_file(reply.content, cleanup)
        else:
            path = self._local_path(reply.content, cleanup, _MAX_MEDIA_BYTES[kind])
        filename = getattr(reply, "file_name", "") or os.path.basename(path)
        # ChatChannel already sends an IMAGE_URL's text first; only FILE carries a caption here.
        caption = (getattr(reply, "text_content", None) or "") if rtype == ReplyType.FILE else ""
        if len(caption) > CAPTION_LIMIT:
            return [self._media_unit(path, kind, filename=filename)] + self._text_units(caption)
        return [self._media_unit(path, kind, caption=caption, filename=filename)]

    @staticmethod
    def _text_units(text: str) -> list:
        return [("text", (lambda c=chunk: {"body": c})) for chunk in split_markdown(text, TEXT_LIMIT) if chunk]

    @staticmethod
    def _upload_mime(path: str, kind: str, filename: str):
        """The MIME Meta will accept for *path* as *kind*, or ``None`` if it accepts none."""
        mime = mimetypes.guess_type(filename)[0] or mimetypes.guess_type(path)[0] or ""
        if kind == "image" and mime not in _IMAGE_MIMES:
            with open(path, "rb") as f:
                mime = _sniff_image_mime(f.read(16))
        if mime in _ALLOWED_MIMES[kind]:
            return mime
        if kind == "document" and (mime.startswith("text/") or mime in _TEXT_LIKE_MIMES
                                   or os.path.splitext(filename)[1].lower() in _TEXT_LIKE_EXTS):
            return "text/plain"
        return None

    def _media_unit(self, path: str, kind: str, caption: str = "", filename: str = ""):
        filename = filename or os.path.basename(path)
        mime = self._upload_mime(path, kind, filename)
        if mime is None:
            logger.warning(f"[WhatsApp] {kind} {filename!r} is a type WhatsApp does not accept; sending a notice")
            notice = _t("WhatsApp 不支持发送此文件类型：", "WhatsApp can't deliver this file type: ") + filename
            return "text", (lambda: {"body": notice})
        size = os.path.getsize(path)
        if size > _MAX_MEDIA_BYTES[kind]:
            # Meta refuses it with a non-retryable 400; tell the user instead of going silent.
            logger.warning(f"[WhatsApp] {kind} {filename!r} is {size} bytes, over WhatsApp's "
                           f"{_MAX_MEDIA_BYTES[kind]}-byte limit; sending a notice")
            notice = _t("文件超过 WhatsApp 的大小限制：", "File exceeds WhatsApp's size limit: ") + filename
            return "text", (lambda: {"body": notice})

        def body():
            payload = {"id": self.client.upload_media(path, mime)}
            if kind == "document":
                payload["filename"] = filename
            if caption and kind != "audio":
                payload["caption"] = caption
            return payload

        return kind, body

    @staticmethod
    def _bytes_to_file(content, cleanup: list) -> str:
        data = content.getvalue() if isinstance(content, io.BytesIO) else bytes(content)
        ext = ".png" if _sniff_image_mime(data) == "image/png" else ".jpg"
        path = _tmp_path("whatsapp_out", ext)
        with open(path, "wb") as f:
            f.write(data)
        cleanup.append(path)
        return path

    @staticmethod
    def _local_path(content, cleanup: list, max_bytes: int) -> str:
        ref = str(content or "")
        if ref.startswith("file://"):
            return ref[len("file://"):]
        if ref.startswith(("http://", "https://")):
            ext = os.path.splitext(urlparse(ref).path)[1] or ".bin"
            path = _tmp_path("whatsapp_remote", ext)
            cleanup.append(path)
            # The URL came from a model reply: SSRF-guard it and every redirect.
            download_to_file(ref, path, max_bytes, timeout=(5, 60),
                             max_seconds=_MAX_REMOTE_MEDIA_SECONDS, guarded=True)
            return path
        if not ref:
            raise ValueError("empty media reference")
        return ref
