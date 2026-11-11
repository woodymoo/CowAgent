# WhatsApp Channel — Architecture Design

English | [中文](zh/03-architecture.md)

Inputs: `01-requirements.md`, `02-codebase-analysis.md`.

## 1. Principles applied
- **Single responsibility**: transport, policy (throttle/retry), protocol parsing, domain mapping, group operations and lifecycle each live in their own module.
- **Dependency inversion / testability**: every module that does I/O takes its collaborators (HTTP session, clock, sleep) by injection, so tests never touch the network or sleep.
- **Open/closed**: the existing framework is extended only at its registration points. No base-class edits.
- **Fail fast, never silent**: bad config fails `startup()` through `report_startup_error`. Each webhook payload error is logged with context and never crashes the server.

## 2. Module layout

```
channel/whatsapp/
├── __init__.py
├── whatsapp_api.py          # WhatsAppApiClient + WhatsAppApiError  (transport only)
├── whatsapp_rate_limiter.py # TokenBucket, SendThrottle             (pure policy, injectable clock)
├── whatsapp_sender.py       # WhatsAppSender: throttle → client → retry/backoff
├── whatsapp_webhook.py      # verify_signature, verify_subscription, parse_webhook → dataclasses
├── whatsapp_server.py       # web.py app factory bound to ONE channel instance
├── whatsapp_message.py      # WhatsAppMessage(ChatMessage) from InboundMessage
├── whatsapp_groups.py       # WhatsAppGroupService (Groups API)
└── whatsapp_channel.py      # WhatsAppChannel(ChatChannel): lifecycle, inbound, send()
agent/tools/whatsapp_group/
├── __init__.py
└── whatsapp_group.py        # WhatsAppGroupTool → running channel's group_service
```

Dependency direction (no cycles):

```
whatsapp_channel ─┬─> whatsapp_server ─> whatsapp_webhook
                  ├─> whatsapp_message ─> whatsapp_webhook (InboundMessage)
                  ├─> whatsapp_sender ──> whatsapp_api, whatsapp_rate_limiter
                  └─> whatsapp_groups ──> whatsapp_api
agent/tools/whatsapp_group ─> common.channel_registry (runtime lookup) ─> channel.group_service
```

## 3. Components

### 3.1 `WhatsAppApiClient` (whatsapp_api.py)
```python
class WhatsAppApiError(Exception):
    http_status: int; code: Optional[int]; title: str; message: str; details: str
    retryable: bool  # property: http 429/5xx or code in {130429, 131056, 80007, 4, 2}
class WhatsAppApiClient:
    def __init__(self, access_token, phone_number_id, api_version="v21.0",
                 session=None, base_url="https://graph.facebook.com", timeout=(5, 30))
    def request(self, method, path, *, json=None, params=None, data=None, files=None) -> dict
    def send_message(self, payload: dict) -> dict          # POST /{pnid}/messages
    def upload_media(self, file_path, mime_type) -> str    # POST /{pnid}/media → id
    def get_media_url(self, media_id) -> str               # GET /{media_id}
    def download_media(self, url, dest_path, max_bytes) -> None  # bearer GET via common.media_download
```
- Parses Graph error envelopes (`{"error": {code, message, error_user_title, error_data.details}}`) into `WhatsAppApiError`.
- Never logs the token. Error text passes through `scrub_secrets`.

### 3.2 Rate limiting (whatsapp_rate_limiter.py)
```python
class TokenBucket:   # capacity, refill_per_sec, clock=time.monotonic
    def try_acquire(self) -> float   # 0.0 if acquired, else seconds to wait (no token taken)
class SendThrottle:
    def __init__(self, global_rate, pair_interval, pair_burst, clock=..., sleep=..., max_pairs=10_000)
    def acquire(self, recipient: str) -> float  # blocks until BOTH buckets grant; returns total waited
```
- Global bucket: capacity = `global_rate`, refill = `global_rate`/s.
- Pair bucket per recipient: capacity = `pair_burst`, refill = 1/`pair_interval`. Pair buckets live in an `OrderedDict` capped at `max_pairs`, LRU evicted.
- Acquisition order: pair first, then global. That way a slow pair never holds a global token while it sleeps. A single lock guards bucket maths, and **sleeping happens outside the lock**.

### 3.3 `WhatsAppSender` (whatsapp_sender.py)
```python
class WhatsAppSender:
    def __init__(self, client, throttle, max_attempts=3, base_delay=1.0, sleep=time.sleep, rand=random.random)
    def send(self, recipient, recipient_type, msg_type, body: dict) -> dict
```
- Builds `{"messaging_product":"whatsapp","recipient_type":…, "to":…, "type":msg_type, msg_type: body}`.
- Loop: `throttle.acquire(to)` → `client.send_message()`. On `WhatsAppApiError.retryable`, back off by `base_delay * 2**attempt * (0.5 + rand())` and retry. Non-retryable errors are re-raised at once. `131048` and `131047` are logged at error level with an operator hint.

### 3.4 Webhook protocol (whatsapp_webhook.py)
Pure functions and frozen dataclasses, with no web.py and no channel:
```python
def verify_subscription(mode, token, challenge, expected_token) -> Optional[str]
def verify_signature(app_secret: str, raw_body: bytes, header: str) -> bool  # hmac.compare_digest
class WebhookSchemaError(ValueError)
@dataclass(frozen=True) class InboundMessage: msg_id, timestamp, msg_type, sender_id, sender_name,
    group_id (Optional), text, caption, media_id, mime_type, filename
@dataclass(frozen=True) class StatusEvent: msg_id, status, recipient_id, errors
@dataclass(frozen=True) class GroupEvent: field, value(dict)
@dataclass(frozen=True) class WebhookBatch: messages, statuses, group_events
def parse_webhook(payload: dict, phone_number_id: str) -> WebhookBatch
```
- Schema validation at the boundary: `object` must be `whatsapp_business_account`, and `entry`/`changes` must be lists. Malformed elements are skipped and logged, not fatal to the batch.
- `messages`-field changes whose `metadata.phone_number_id` ≠ ours are ignored.
- `sender_id = from or from_user_id`. `sender_name` comes from `contacts[]` matched by `wa_id`/`user_id`.

### 3.5 HTTP server (whatsapp_server.py)
```python
def build_wsgi_app(on_verify, on_event) -> web.application
class WebhookServer:  # wraps web.httpserver.WSGIServer; start() blocks, stop()
```
- The route `/whatsapp/webhook` maps to a handler class **created per instance** (a closure over the callbacks). This fixes the singleton coupling of the wechat_kf pattern and allows multi-instance setups on different ports.
- `POST`: read the raw body, call `on_event(raw, signature_header)`, which returns an HTTP status (200/400/403). The channel does the async dispatch.

### 3.6 `WhatsAppMessage` (whatsapp_message.py)
Maps `InboundMessage` (+ local media path) → `ChatMessage` fields:
- 1:1: `other_user_id = sender_id`.
- Group: `is_group=True`, `other_user_id = group_id`, `actual_user_id = sender_id`, `is_at = True` (the trigger is decided before construction).

### 3.7 `WhatsAppGroupService` (whatsapp_groups.py)
One method per Groups API endpoint (create, list, info, update, delete, get/reset invite link, remove participants, list/approve/reject join requests). Arguments are validated before the call: subject ≤128 non-empty, description ≤2048, participants 1–8, and join_approval_mode in an allowed set. Returns Graph JSON. Errors propagate as `WhatsAppApiError`.

### 3.8 `WhatsAppChannel` (whatsapp_channel.py)
`@singleton class WhatsAppChannel(ChatChannel)`, `NOT_SUPPORT_REPLYTYPE = []`.
- `__init__`: dedup `ExpiredDict(3600)`, executor (4 workers), and the same conf neutralisation as Telegram (`group_name_white_list=["ALL_GROUP"]`, `single_chat_prefix=[""]`).
- `startup()`: reads creds through `self.cfg`. If any of token / phone_number_id / app_secret / verify_token is missing → `report_startup_error`. Then builds client, throttle, sender and group_service, starts `WebhookServer` on `whatsapp_port`, calls `report_startup_success()`, and blocks.
- `stop()`: stops the server and shuts down the executor.
- `_on_verify(mode, token, challenge)` → `verify_subscription`.
- `_on_event(raw, sig)` → 403 if the signature is bad; 200 (logged, not processed) if a signed payload has bad JSON or schema (M4). Otherwise it queues each message into a per-chat FIFO inbox (key: group id or sender id) and returns 200. One drain task per chat runs on the executor, so a chat's messages are handled in arrival order (a text cannot overtake the photo sent before it) while different chats run in parallel. Statuses and group events go to `_process_batch`, which only logs them.
- `_process_batch`: logs statuses (warning on `failed`) and group events, then `_handle_inbound(m)` for each message.
- `_handle_inbound`: dedup → group trigger (`whatsapp_group_trigger`: `all` | `prefix` using `group_chat_prefix`) → media download → file-cache convention → `_compose_context` → set `session_id`, `receiver`, `whatsapp_recipient_type`, `no_need_at=True` (a literal "@name" is not a WhatsApp mention) → `stamp_speaker_from_channel` → `produce`.
- `send(reply, context)`: resolves `(to, recipient_type)` (the context key wins; otherwise a phone-digits regex means individual, anything else means group, which covers scheduler sends) and dispatches by `ReplyType`:
  - text → split at 4096 chars → `sender.send(..., "text", {"body": chunk})`
  - image / file / video / voice → resolve to a local file (`file://`, a path, or a size-capped http download) → `client.upload_media` → `sender.send(..., type, {"id": media_id, ...})`
  - **Retry contract**: raise only if the first unit failed (nothing delivered). Later failures are logged and swallowed *after logging*, which matches ChatChannel `_send` semantics.

### 3.9 `WhatsAppGroupTool` (agent/tools/whatsapp_group/whatsapp_group.py)
- `name = "whatsapp_group"`. The `params` JSON schema has `action` as an enum plus per-action fields.
- `is_available()`: true only if a running WhatsApp channel with a `group_service` exists (looked up through `get_channel_manager().find_channels_by_type("whatsapp")`). If several instances run, it prefers the one bound to the calling agent (`instance_id` param optional).
- `execute(args)` → dispatch table → `ToolResult.success(json)`. A `WhatsAppApiError` becomes `ToolResult.fail` with code, title and message.

## 4. Key flows

**Inbound**
```
Meta ──POST──> WebhookServer ─> channel._on_event(raw,sig)
   verify_signature ✗→403 │ parse_webhook ✗→200 (logged) │ per-chat inbox + executor → 200
_drain_inbox(chat) → _handle_inbound → (download media) → _compose_context → produce()
   → ChatChannel.consume → agent → _send_reply → channel.send()
```
**Outbound**
```
send() → [upload_media] → WhatsAppSender.send → SendThrottle.acquire(to) → client.send_message
      ↖ retry (130429/131056/80007/429/5xx, ≤3, exp backoff + jitter)
```

## 5. Registration edits (outside channel/whatsapp)
`common/const.py` (WHATSAPP), `channel/channel_factory.py`, `app.py::_clear_singleton_cache`, `channel/channel_instances.py` (CREDENTIAL_KEYS / labels / MULTI_INSTANCE_READY), `config.py` defaults and the channel list comment, `channel/web/api/channels.py` (CHANNEL_DEFS + EN_FIRST_CHANNELS), `channel/web/core/channel.py` (banner lists), `agent/tools/__init__.py` (optional import), `docs/channels/whatsapp.mdx` + `docs/docs.json`.

## 6. Config keys
| key | default | note |
|---|---|---|
| whatsapp_access_token | "" | secret, credential |
| whatsapp_phone_number_id | "" | credential |
| whatsapp_app_secret | "" | secret, credential (signature) |
| whatsapp_verify_token | "" | secret, credential (handshake) |
| whatsapp_port | 9893 | local webhook port |
| whatsapp_api_version | "v21.0" | Graph version |
| whatsapp_group_trigger | "all" | `all` \| `prefix` |
| whatsapp_rate_limit_mps | 20 | global msgs/s (Meta max 80) |
| whatsapp_pair_interval_seconds | 6 | per-recipient refill |
| whatsapp_pair_burst | 5 | per-recipient burst |

## 7. Test strategy (TDD order)
| Layer | Test file | Technique |
|---|---|---|
| rate limiter | `tests/test_whatsapp_rate_limiter.py` | fake clock and sleep |
| api client | `tests/test_whatsapp_api.py` | fake `requests.Session` |
| sender | `tests/test_whatsapp_sender.py` | fake client and throttle, assert retry/no-retry |
| webhook | `tests/test_whatsapp_webhook.py` | signature vectors, payload fixtures |
| groups | `tests/test_whatsapp_groups.py` | fake client, validation |
| channel | `tests/test_whatsapp_channel.py` | inbound → produce captured, send dispatch, retry contract |
| tool | `tests/test_whatsapp_group_tool.py` | fake channel manager |
| registration | `tests/test_whatsapp_registration.py` | factory/const/instances/config/console defs |
| **integration** | `tests/test_whatsapp_integration.py` | real `WebhookServer` on an ephemeral port + a local fake Graph HTTP server; signed POST → reply posted to the fake Graph |

## 8. Risks
- Groups API payload shapes beyond the documented fields may change. We return raw JSON to the Agent, so that is tolerated.
- The group-id format is undocumented. The digits heuristic only applies when the context lacks `whatsapp_recipient_type`.
- web.py `WSGIServer` per port: two instances must use different `whatsapp_port`. Port is not a credential key; per-instance `cfg("whatsapp_port")` override is honoured if provided.

---

## 9. Architecture review — resolution (rev 2, authoritative where it differs from §1–8)

Reviewed by the backend-architect agent against the live code. All findings were accepted:

| # | Finding | Resolution |
|---|---|---|
| B1 | A per-instance `whatsapp_port` is unreachable: only `CREDENTIAL_KEYS` are copied per instance, so two instances would collide on one port. | WhatsApp runs **single-instance**, like the other webhook-port channels (`wechat_kf`, `wechatcom_app`). It is **not** added to `channel_instances.py`. The factory builds the plain singleton. |
| B2 | Startup reported success before the port was bound. | `server.prepare()` (cheroot binds here and raises) → on failure `report_startup_error` and return. Otherwise `report_startup_success()`, then `server.serve()`. |
| M1 | Sender retries × `ChatChannel._send` retries multiply (up to 9 attempts, and 131047/131048 get re-sent). | `send()` catches `WhatsAppApiError`. **Non-retryable** errors are logged at error level and the method returns. It raises only when a retryable error was exhausted **and nothing was delivered yet**. |
| M2 | A 1 s backoff is shorter than the 6 s pair window. | On 131056 the sender calls `throttle.penalize(to)`, which drains that recipient's pair bucket, so the next `acquire` waits a full pair interval. |
| M3 | Mutating the global `conf()` (Telegram-style) breaks isolation (NFR-1). | **No conf mutation.** `_compose_context` is overridden like Telegram's (sets session_id/receiver, skips the legacy whitelist, prefix and @ checks). `no_need_at=True` is kept. |
| M4 | Returning 400 for a signed-but-bad payload makes Meta retry for days. | 403 only for a bad or missing signature. A signed payload with bad JSON or schema is logged at error level and gets **200**. `executor.submit` after shutdown is caught. |
| M5 | The request body was read unbounded, before the HMAC check. Also `StaticMiddleware` serves `./static`. | `server.max_request_body_size = 4 MiB` (cheroot answers 413). No `StaticMiddleware`. `autoreload=False`. Handlers have a catch-all so the web.py debug page never renders. |
| M6 | Outbound media URLs come from the model (SSRF). | `download_to_file(..., guarded=True, max_seconds=…)`, as Telegram does. |
| m | Rate-limiter algorithm. | Under the lock, compute both waits. Take both tokens only when both are 0. Otherwise sleep for max(wait) outside the lock and loop. `max_wait` (default 120 s) caps total blocking: once exceeded the throttle **raises `ThrottleTimeout`** (the message is not sent, and the error is logged). This protects the shared 8-worker `handler_pool`. |
| m | The recipient-type heuristic is fragile. | Group receivers are encoded as **`group:<group_id>`** in `context["receiver"]`, so the type survives scheduler round-trips. The digits heuristic is removed. |
| m | Retryable code list. | {130429, 131056, 80007, 4 (app rate limit), 2 (temporary service)} + HTTP 429/5xx/network. FR-6 was updated to match. |
| m | Dedup race. | Check-and-set guarded by a lock. |
| m | Tool details. | `is_available()` never raises. The constructor does no I/O and channel imports are lazy. Exported in `__all__`. **No `instance_id` param** (single-instance). |
| m | Media types. | Outbound images that are not jpeg/png go as `document`. |

Registration edits (final): `common/const.py`, `channel/channel_factory.py`, `app.py::_clear_singleton_cache`, `config.py`, `channel/web/api/channels.py`, `channel/web/core/channel.py`, `agent/tools/__init__.py`, `docs/channels/whatsapp.mdx`, `docs/docs.json`.

## 10. Code review — resolution

Independent code review after implementation (0 critical, 1 high, 3 medium, 4 low). Every finding was verified against the code and fixed test-first:

| # | Finding | Fix |
|---|---|---|
| H1 | Media in formats Meta rejects (gif, wav, zip, …) was uploaded, failed with a non-retryable 400, and the user got nothing. | MIME allowlists per kind, taken from Meta's supported-media table. Text-like files (`.md`, `.csv`, `.json`, …) are sent as `text/plain` documents. Anything else is not uploaded; the user gets a short text notice instead. |
| M1 | A read timeout on `POST /messages` was retried and could duplicate messages. | Only `ConnectionError`/`ConnectTimeout` (the request never left) are retryable. A `ReadTimeout` is not. |
| M2 | `whatsapp_rate_limit_mps` < 1 blocked every send. | Global bucket capacity is `max(1, rate)`. |
| M3 | A `group_id` that passed the character check could be any Graph node (`me`, `.`, the WABA id). | Reserved nodes and our own phone_number_id are rejected. `delete`/`remove_participants` first confirm the node is a group (`GET ?fields=subject`). |
| L1 | A non-ASCII signature or verify token made `compare_digest` raise, giving a 500 and a logged traceback. | Values are compared as bytes and refused cleanly. |
| L2 | Invite links were logged at INFO. | Group events log only the field and group ids. |
| L3 | Remote media was downloaded up to 100 MB before Meta rejected it. | Download caps follow Meta's per-kind limits (5 MB images, 16 MB audio/video, 100 MB documents). |
| L4 | Spec documents sit inside the runtime package. | Placement is left to the maintainer (see PR notes). |
