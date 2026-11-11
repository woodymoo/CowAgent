"""WhatsApp Cloud API webhook protocol.

Pure functions only, with no HTTP framework and no channel state:

* ``verify_subscription``: Meta's GET handshake (``hub.mode`` / ``hub.verify_token``).
* ``verify_signature``: ``X-Hub-Signature-256``, HMAC-SHA256 of the *raw* body keyed with the app secret.
* ``parse_webhook``: payload → typed, immutable events. The top-level shape is
  validated (``WebhookSchemaError``). A single malformed element is logged and
  skipped, so one bad item never drops the rest of the batch.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Optional, Tuple

from common.log import logger

GROUP_FIELDS = frozenset({
    "group_lifecycle_update",
    "group_participants_update",
    "group_settings_update",
    "group_status_update",
})
MEDIA_TYPES = ("image", "audio", "document", "video")


class WebhookSchemaError(ValueError):
    """The payload is not a WhatsApp Business Account webhook."""


@dataclass(frozen=True)
class InboundMessage:
    msg_id: str
    sender_id: str
    msg_type: str
    timestamp: int = 0
    sender_name: str = ""
    group_id: Optional[str] = None
    text: str = ""
    caption: str = ""
    media_id: str = ""
    mime_type: str = ""
    filename: str = ""


@dataclass(frozen=True)
class StatusEvent:
    msg_id: str
    status: str
    recipient_id: str = ""
    errors: Tuple[dict, ...] = ()


@dataclass(frozen=True)
class GroupEvent:
    field: str
    value: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookBatch:
    messages: Tuple[InboundMessage, ...] = ()
    statuses: Tuple[StatusEvent, ...] = ()
    group_events: Tuple[GroupEvent, ...] = ()


def verify_subscription(mode, token, challenge, expected_token) -> Optional[str]:
    """Return the challenge to echo when the handshake is valid, else ``None``."""
    if mode != "subscribe" or not expected_token or not token:
        return None
    if not hmac.compare_digest(str(token).encode("utf-8"), str(expected_token).encode("utf-8")):
        return None
    return challenge


def verify_signature(app_secret: str, raw_body: bytes, header: Optional[str]) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # Compare bytes: compare_digest raises TypeError on non-ASCII str input.
    return hmac.compare_digest(header[len("sha256="):].encode("utf-8"), expected.encode("ascii"))


def parse_webhook(payload, phone_number_id: str) -> WebhookBatch:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise WebhookSchemaError("not a whatsapp_business_account webhook")
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise WebhookSchemaError("'entry' must be a list")

    messages, statuses, group_events = [], [], []
    for change in _iter_changes(entries):
        field_name = change.get("field")
        value = change.get("value")
        if not isinstance(value, dict) or not _is_ours(value, phone_number_id):
            continue
        if field_name == "messages":
            names = _contact_names(value.get("contacts"))
            messages.extend(_parse_messages(value.get("messages"), names))
            statuses.extend(_parse_statuses(value.get("statuses")))
        elif field_name in GROUP_FIELDS:
            group_events.append(GroupEvent(field=field_name, value=value))
    return WebhookBatch(tuple(messages), tuple(statuses), tuple(group_events))


def _iter_changes(entries):
    for entry in entries:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list):
            logger.warning("[WhatsApp] webhook entry without a changes list skipped")
            continue
        for change in changes:
            if isinstance(change, dict):
                yield change


def _is_ours(value: dict, phone_number_id: str) -> bool:
    """Several numbers can share one Meta app. Drop events addressed to another number."""
    metadata = value.get("metadata")
    pnid = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
    if pnid is None:
        return True
    return str(pnid) == str(phone_number_id)


def _contact_names(contacts) -> dict:
    names = {}
    for contact in contacts if isinstance(contacts, list) else []:
        if not isinstance(contact, dict):
            continue
        profile = contact.get("profile")
        name = profile.get("name", "") if isinstance(profile, dict) else ""
        for key in ("wa_id", "user_id"):
            if contact.get(key):
                names[str(contact[key])] = name
    return names


def _parse_messages(raw, names):
    out = []
    for item in raw if isinstance(raw, list) else []:
        msg = _parse_message(item, names) if isinstance(item, dict) else None
        if msg is None:
            logger.warning("[WhatsApp] malformed inbound message skipped: id={}".format(
                item.get("id") if isinstance(item, dict) else None))
            continue
        out.append(msg)
    return out


def _parse_message(item: dict, names: dict) -> Optional[InboundMessage]:
    msg_id = item.get("id")
    sender = item.get("from") or item.get("from_user_id")
    msg_type = item.get("type")
    if not msg_id or not sender or not msg_type:
        return None
    try:
        timestamp = int(item.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    sender = str(sender)
    fields = dict(
        msg_id=str(msg_id),
        sender_id=sender,
        msg_type=str(msg_type),
        timestamp=timestamp,
        sender_name=names.get(sender) or names.get(str(item.get("from_user_id") or "")) or "",
        group_id=str(item["group_id"]) if item.get("group_id") else None,
    )
    body = item.get(msg_type)
    body = body if isinstance(body, dict) else {}
    if msg_type == "text":
        fields["text"] = str(body.get("body") or "")
    elif msg_type in MEDIA_TYPES:
        fields.update(
            media_id=str(body.get("id") or ""),
            mime_type=str(body.get("mime_type") or ""),
            caption=str(body.get("caption") or ""),
            filename=str(body.get("filename") or ""),
        )
    return InboundMessage(**fields)


def _parse_statuses(raw):
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        errors = item.get("errors")
        out.append(StatusEvent(
            msg_id=str(item["id"]),
            status=str(item.get("status") or ""),
            recipient_id=str(item.get("recipient_id") or ""),
            errors=tuple(e for e in errors if isinstance(e, dict)) if isinstance(errors, list) else (),
        ))
    return out
