"""Thin transport client for the WhatsApp Cloud API (Meta Graph API).

Only transport lives here: URL building, bearer auth, timeouts, and mapping
Graph error envelopes to ``WhatsAppApiError``. Throttling and retries are
policy and live in ``whatsapp_sender``. Group operations live in
``whatsapp_groups``.
"""

import os
from typing import Optional

import requests

from common.media_download import download_to_file
from common.utils import scrub_secrets

GRAPH_BASE_URL = "https://graph.facebook.com"
DEFAULT_TIMEOUT = (5, 30)
_UPLOAD_TIMEOUT = (5, 120)

# Throttling codes that clear on their own, so a backed-off retry is correct.
# 131048 (spam rate limit) and 131047 (24h window) are deliberately excluded:
# retrying them only makes the account look worse.
RETRYABLE_CODES = frozenset({130429, 131056, 80007, 4, 2})
CODE_PAIR_RATE_LIMIT = 131056
CODE_SPAM_RATE_LIMIT = 131048
CODE_WINDOW_CLOSED = 131047


class WhatsAppApiError(Exception):
    def __init__(self, http_status: int, code: Optional[int] = None, message: str = "",
                 title: str = "", details: str = "", retryable: Optional[bool] = None):
        self._retryable = retryable
        self.http_status = http_status
        self.code = code
        self.message = message
        self.title = title
        self.details = details
        super().__init__(self._describe())

    def _describe(self) -> str:
        parts = [f"http={self.http_status}"]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.title:
            parts.append(f"title={self.title}")
        if self.message:
            parts.append(f"message={self.message}")
        if self.details:
            parts.append(f"details={self.details}")
        return "WhatsApp API error: " + ", ".join(parts)

    @property
    def retryable(self) -> bool:
        if self._retryable is not None:
            return self._retryable
        if self.code in (CODE_SPAM_RATE_LIMIT, CODE_WINDOW_CLOSED):
            return False
        if self.code in RETRYABLE_CODES:
            return True
        return self.http_status == 429 or self.http_status >= 500

    def to_dict(self) -> dict:
        return {"http_status": self.http_status, "code": self.code, "title": self.title,
                "message": self.message, "details": self.details}


class WhatsAppApiClient:
    def __init__(self, access_token: str, phone_number_id: str, api_version: str = "v21.0",
                 session=None, base_url: str = GRAPH_BASE_URL, timeout=DEFAULT_TIMEOUT):
        if not access_token or not phone_number_id:
            raise ValueError("access_token and phone_number_id are required")
        self._token = access_token
        self.phone_number_id = str(phone_number_id)
        self._prefix = f"{base_url.rstrip('/')}/{api_version}"
        self._session = session or requests.Session()
        self._timeout = timeout

    # -- generic ---------------------------------------------------------------

    def request(self, method: str, path: str, *, json=None, params=None, data=None, files=None,
                timeout=None) -> dict:
        url = f"{self._prefix}/{path.lstrip('/')}"
        kwargs = {"headers": {"Authorization": f"Bearer {self._token}"},
                  "timeout": timeout or self._timeout}
        for key, value in (("json", json), ("params", params), ("data", data), ("files", files)):
            if value is not None:
                kwargs[key] = value
        try:
            response = self._session.request(method, url, **kwargs)
        except requests.RequestException as e:
            # Retry only when the request never reached Meta. After a read timeout
            # Meta may already have accepted the message, and a retry would duplicate it.
            never_sent = isinstance(e, requests.ConnectionError) or isinstance(e, requests.ConnectTimeout)
            raise WhatsAppApiError(0, message=self._scrub(f"{type(e).__name__}: {e}"),
                                   retryable=never_sent) from e
        return self._decode(response)

    def _scrub(self, text) -> str:
        return scrub_secrets(str(text)).replace(self._token, "***")

    def _decode(self, response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        if 200 <= response.status_code < 300:
            return body if isinstance(body, dict) else {}
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            data = error.get("error_data") if isinstance(error.get("error_data"), dict) else {}
            raise WhatsAppApiError(
                response.status_code,
                code=error.get("code"),
                message=self._scrub(error.get("message", "")),
                title=self._scrub(error.get("error_user_title") or error.get("title") or ""),
                details=self._scrub(data.get("details", "")),
            )
        raise WhatsAppApiError(response.status_code, message=self._scrub((response.text or "")[:500]))

    # -- messaging ---------------------------------------------------------------

    def send_message(self, payload: dict) -> dict:
        return self.request("POST", f"{self.phone_number_id}/messages", json=payload)

    def upload_media(self, file_path: str, mime_type: str) -> str:
        with open(file_path, "rb") as fh:
            body = self.request(
                "POST", f"{self.phone_number_id}/media",
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (os.path.basename(file_path), fh, mime_type)},
                timeout=_UPLOAD_TIMEOUT,
            )
        media_id = body.get("id")
        if not media_id:
            raise WhatsAppApiError(200, message="media upload returned no id")
        return str(media_id)

    def get_media_url(self, media_id: str) -> str:
        url = self.request("GET", str(media_id)).get("url")
        if not url:
            raise WhatsAppApiError(200, message=f"media {media_id} has no url")
        return url

    def download_media(self, url: str, dest_path: str, max_bytes: int) -> None:
        """Media URLs need the same bearer token. The download is streamed and size-capped."""
        download_to_file(url, dest_path, max_bytes=max_bytes, timeout=(5, 60), max_seconds=300,
                         headers={"Authorization": f"Bearer {self._token}"})
