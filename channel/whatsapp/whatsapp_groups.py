"""WhatsApp Groups API operations.

Platform limits (Meta, Groups API): Official Business Accounts only, at most 8
participants, and people join only through the invite link (there is no
add-participant endpoint). Arguments are validated here so obvious mistakes
never cost an API call. Meta's own errors propagate as ``WhatsAppApiError``.
"""

import re
from typing import Iterable, Optional

MAX_SUBJECT = 128
MAX_DESCRIPTION = 2048
MAX_PARTICIPANTS = 8
JOIN_APPROVAL_MODES = ("auto_approve", "approval_required")
INFO_FIELDS = ("subject", "description", "participants", "join_approval_mode", "suspended",
               "creation_timestamp", "total_participant_count")
# Group ids end up in the URL path. Accept only opaque id characters so a
# model-supplied value cannot walk to another Graph node ("../me").
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_.@=-]+$")
_PRODUCT = {"messaging_product": "whatsapp"}


_RESERVED_NODES = frozenset({"me", "."})


def _group_id(group_id, phone_number_id: str = "") -> str:
    gid = str(group_id or "").strip()
    if (not gid or ".." in gid or not _GROUP_ID_RE.match(gid)
            or gid.lower() in _RESERVED_NODES or gid == str(phone_number_id)):
        raise ValueError(f"invalid group_id: {group_id!r}")
    return gid


def _subject(subject) -> str:
    text = str(subject or "").strip()
    if not text:
        raise ValueError("subject must not be empty")
    if len(text) > MAX_SUBJECT:
        raise ValueError(f"subject longer than {MAX_SUBJECT} characters")
    return text


def _description(description) -> str:
    text = str(description)
    if len(text) > MAX_DESCRIPTION:
        raise ValueError(f"description longer than {MAX_DESCRIPTION} characters")
    return text


def _id_list(values, name, limit=None) -> list:
    if isinstance(values, str) or not isinstance(values, Iterable):
        raise ValueError(f"{name} must be a list")
    items = [str(v).strip() for v in values]
    if not items or any(not v for v in items):
        raise ValueError(f"{name} must be a non-empty list of non-empty values")
    if limit and len(items) > limit:
        raise ValueError(f"at most {limit} {name} per call")
    return items


class WhatsAppGroupService:
    def __init__(self, client):
        self._client = client

    @property
    def _pnid(self) -> str:
        return self._client.phone_number_id

    def _gid(self, group_id) -> str:
        return _group_id(group_id, self._pnid)

    def _confirmed_group(self, group_id) -> str:
        """Destructive calls go to arbitrary Graph node ids with the system-user token.
        Make sure the target really is a group (groups expose a ``subject``) first."""
        gid = self._gid(group_id)
        node = self._client.request("GET", gid, params={"fields": "subject"})
        if not isinstance(node, dict) or "subject" not in node:
            raise ValueError(f"{gid} is not a WhatsApp group of this number")
        return gid

    def create(self, subject, description: Optional[str] = None,
               join_approval_mode: Optional[str] = None) -> dict:
        body = dict(_PRODUCT, subject=_subject(subject))
        if description:
            body["description"] = _description(description)
        if join_approval_mode:
            if join_approval_mode not in JOIN_APPROVAL_MODES:
                raise ValueError(f"join_approval_mode must be one of {JOIN_APPROVAL_MODES}")
            body["join_approval_mode"] = join_approval_mode
        return self._client.request("POST", f"{self._pnid}/groups", json=body)

    def list_groups(self, limit: int = 25, after: Optional[str] = None,
                    before: Optional[str] = None) -> dict:
        if not 1 <= int(limit) <= 1024:
            raise ValueError("limit must be between 1 and 1024")
        params = {"limit": int(limit)}
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        return self._client.request("GET", f"{self._pnid}/groups", params=params)

    def info(self, group_id) -> dict:
        return self._client.request("GET", self._gid(group_id), params={"fields": ",".join(INFO_FIELDS)})

    def update(self, group_id, subject: Optional[str] = None, description: Optional[str] = None) -> dict:
        gid = self._gid(group_id)
        body = dict(_PRODUCT)
        if subject is not None:
            body["subject"] = _subject(subject)
        if description is not None:
            body["description"] = _description(description)
        if len(body) == 1:
            raise ValueError("nothing to update: give subject and/or description")
        return self._client.request("POST", gid, json=body)

    def delete(self, group_id) -> dict:
        return self._client.request("DELETE", self._confirmed_group(group_id))

    def get_invite_link(self, group_id) -> dict:
        return self._client.request("GET", f"{self._gid(group_id)}/invite_link")

    def reset_invite_link(self, group_id) -> dict:
        return self._client.request("POST", f"{self._gid(group_id)}/invite_link", json=dict(_PRODUCT))

    def remove_participants(self, group_id, users) -> dict:
        items = _id_list(users, "participants", MAX_PARTICIPANTS)
        gid = self._confirmed_group(group_id)
        participants = [{"user": u.lstrip("+")} for u in items]
        return self._client.request("DELETE", f"{gid}/participants",
                                    json=dict(_PRODUCT, participants=participants))

    def list_join_requests(self, group_id) -> dict:
        return self._client.request("GET", f"{self._gid(group_id)}/join_requests")

    def approve_join_requests(self, group_id, request_ids) -> dict:
        gid = self._gid(group_id)
        return self._client.request("POST", f"{gid}/join_requests",
                                    json=dict(_PRODUCT, join_requests=_id_list(request_ids, "join_requests")))

    def reject_join_requests(self, group_id, request_ids) -> dict:
        gid = self._gid(group_id)
        return self._client.request("DELETE", f"{gid}/join_requests",
                                    json=dict(_PRODUCT, join_requests=_id_list(request_ids, "join_requests")))
