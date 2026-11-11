# WhatsApp Channel — Requirement Spec

English | [中文](zh/01-requirements.md)

Status: reviewed (rev 2). §5 lists the original requests that were changed or dropped.

## 1. Goal

Add a `whatsapp` channel so that a CowAgent Agent can act as a WhatsApp business number:
it receives messages, replies, and creates and manages WhatsApp groups. Integration goes
**only** through the official **Meta WhatsApp Business Cloud API** (Graph API). No browser
automation, no WhatsApp Web protocol and no unofficial libraries.

## 2. Preconditions (operator setup, outside the code)

| # | Precondition |
|---|---|
| P1 | A Meta Business portfolio, a Meta Developer App with the WhatsApp product, and a WhatsApp Business Account (WABA). |
| P2 | The phone number is registered to the Cloud API, and its **WhatsApp Business profile** (display name, about, etc.) is set up and approved. |
| P3 | A permanent **System User access token** with `whatsapp_business_messaging` (and `whatsapp_business_management` for groups). |
| P4 | A **public HTTPS URL** that forwards to the channel's local webhook port (reverse proxy or tunnel). Meta only calls HTTPS endpoints that have a valid certificate. |
| P5 | The webhook is subscribed to the `messages` field, plus the `group_*` fields if groups are used. |
| P6 (groups only) | The business is an **Official Business Account (OBA)**. Meta returns an eligibility error otherwise. |

## 3. Functional requirements

### FR-1 Configuration
- New config keys (all `whatsapp_` prefixed): `whatsapp_access_token`, `whatsapp_phone_number_id`,
  `whatsapp_app_secret`, `whatsapp_verify_token`, `whatsapp_port` (default 9893),
  `whatsapp_api_version` (default `v21.0`), plus the rate-limit keys in FR-6.
- Secrets can be overridden through environment variables, using the existing config env-override mechanism.
- The channel is selectable through `channel_type: "whatsapp"` and appears in the Web console channel list. It is single-instance, like the other webhook-port channels (see 03 §9 B1).
- Startup fails fast with a clear error if a required key is missing.

### FR-2 Webhook endpoint
- `GET /whatsapp/webhook`: Meta verification handshake. Echo `hub.challenge` when `hub.mode == subscribe` and `hub.verify_token` matches. Otherwise return 403.
- `POST /whatsapp/webhook`: verify `X-Hub-Signature-256` (HMAC-SHA256 of the raw body, keyed with the app secret) and return 403 on mismatch. Validate the payload schema (`object == whatsapp_business_account`, `entry[].changes[]`), ack `200` immediately, then process asynchronously.
- Deduplicate on the WhatsApp message id (Meta re-delivers).
- Ignore events for a different `phone_number_id` (several numbers can share one app).

### FR-3 Receive messages
- 1:1 and group inbound messages of type `text`, `image`, `audio` (voice), `document` and `video` become `ChatMessage`s and enter the standard `ChatChannel.produce()` pipeline.
- Media is downloaded through `GET /{media-id}` then the media URL (bearer token), size-capped, and saved to the workspace tmp dir with a safe filename.
- Images and files follow the same file-cache convention as Telegram: a standalone media message is cached and attached to the next text message. Media with a caption becomes one query.
- Group messages: `messages[].group_id` identifies the group, and `from` / `from_user_id` identifies the sender.
- Unsupported types (stickers, reactions, location, interactive, …) are logged at debug level and skipped.
- `statuses` webhooks (sent / delivered / read / failed) are logged. A `failed` status logs the error code and title at warning level.

### FR-4 Send messages
- Reply types: `TEXT`/`ERROR`/`INFO` (split at the 4096-char limit), `IMAGE_URL`/`IMAGE`, `FILE`, `VIDEO`/`VIDEO_URL`, `VOICE` (sent as audio).
- Media goes out by uploading to `POST /{phone-number-id}/media`, then sending by media id.
- 1:1 messages use `recipient_type: individual` with `to` = wa_id. Group messages use `recipient_type: group` with `to` = group_id.
- Proactive delivery (scheduler) works through the same `send()`, subject to the 24-hour rule (§4).

### FR-5 Group management (Groups API) — exposed to the Agent as a tool
A `whatsapp_group` agent tool, available only while a WhatsApp channel is running, supports:
`create` (subject, description, join_approval_mode) · `list` · `info` · `update` (subject/description) ·
`get_invite_link` · `reset_invite_link` · `remove_participants` · `list_join_requests` ·
`approve_join_requests` · `reject_join_requests` · `delete`.
- After `create`, the invite link arrives asynchronously in a `group_lifecycle_update` webhook. The tool returns the group id when Meta supplies one, and the Agent can then call `get_invite_link`.
- `group_*` webhooks are logged (lifecycle, participants, settings). They are not turned into chat messages.
- Meta's errors (code, title, message) are returned verbatim to the Agent so it can explain them, for example "not OBA".

### FR-6 Anti-ban / rate limiting
- **Global throughput**: a token bucket per phone number, `whatsapp_rate_limit_mps` (default 20, Meta's hard limit is 80).
- **Per-recipient pacing**: a token bucket per recipient (user or group), refilling 1 token every `whatsapp_pair_interval_seconds` (default 6) with burst `whatsapp_pair_burst` (default 5). This follows Meta's pair rate limit of about 1 message per 6 seconds per user, with short bursts allowed.
- Senders **block and wait** for tokens. Messages are never dropped.
- **Retry with exponential backoff and jitter** on throttling and transient errors `130429` (throughput), `131056` (pair rate; the recipient bucket is also drained so the next try waits a full pair interval), `80007` (WABA rate), `4` (app rate), `2` (temporary), and on HTTP 429/5xx/network errors, at most 3 attempts. A single send blocks on the throttle for at most `whatsapp_max_wait_seconds` (default 120).
- **No retry** on `131048` (spam rate limit, a quality signal) or `131047` (24h window expired). Log them at error level with remediation advice.
- Inbound messages get no artificial delay. Webhook handling is ack-first, async-later, so Meta never sees timeouts.

## 4. Platform constraints the operator must know (documented, not worked around)
- **24-hour customer service window**: free-form messages are only allowed within 24h of the user's last message. Outside it, Meta requires an approved template, which this channel does not send (error `131047` is logged).
- **Groups API**: OBA only, max **8 participants**, participants join **only via the invite link** (no add-participant endpoint), not available for WhatsApp Business *app* (coexistence) numbers.
- **Ban risk** on the Cloud API comes mainly from the **quality rating** (users blocking or reporting) and from policy violations, not from speed alone. Rate limiting reduces throttling errors and spam-like bursts, but it cannot guarantee that an account is never restricted.

## 5. Review: revised / removed items

| Original request | Decision | Reason |
|---|---|---|
| "Take over a human-registered account and impersonate it" | **Revised**: the Agent acts *as the business number*, under its own approved Business profile. | Cloud API numbers are business identities. Messages show the business display name. Passing the account off as a private person violates WhatsApp Business policy and is not technically possible through the Cloud API. |
| Keep using the number in the personal WhatsApp app at the same time | **Removed** | Registering a number to the Cloud API takes it off the consumer app. |
| "Create group and manage" (add members freely) | **Revised** to what the Groups API supports: create, invite link, join-request approval, remove, update, delete. | No add-participant endpoint exists, there is an 8-member cap, and OBA is required. |
| "Never get banned" | **Revised** to "stay within Meta's published limits and fail safe" (FR-6). | No client-side mechanism can guarantee this (§4). |
| Avoid Chrome/web automation | **Kept**, inherent in the Cloud API design. | — |
| Sending templates / marketing broadcasts | **Out of scope** | Not requested. It would be a new feature (constraint 1). |
| Group profile picture upload, pin messages, read receipts, typing indicators | **Out of scope** | Not needed for "send/receive/manage". Keeps the change minimal. |

## 6. Non-functional requirements
- **NFR-1 Isolation**: no behaviour change to existing channels. Edits outside `channel/whatsapp/` are limited to registration points (factory, constants, config defaults, channel lists, tool registry).
- **NFR-2 Security**: signature verification, constant-time compare, no secrets in logs (`scrub_secrets`), size-capped downloads, safe filenames.
- **NFR-3 Testability**: HTTP goes through one injectable client, and tests run offline (no Meta calls).
- **NFR-4 Reliability**: webhook handler never raises to the server. Errors are logged with context (no silent swallowing).
- **NFR-5 Compatibility**: Python ≥3.9, existing deps only (`requests`, `web.py`).

## 7. Acceptance criteria
1. Unit tests cover the client, rate limiter, webhook verification and parsing, message mapping, send dispatch, group service and tool.
2. An integration test drives a real local webhook HTTP server end-to-end: signed POST → `produce()` → `send()` → mocked Graph API, plus signature rejection and the verify handshake.
3. The full existing test suite still passes and `ruff` reports no F-errors in new files.
