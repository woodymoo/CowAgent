# Codebase Analysis — Channel Subsystem (as relevant to WhatsApp)

English | [中文](zh/02-codebase-analysis.md)

## 1. Top-level layout

| Path | Role |
|---|---|
| `app.py` | Entry point. `ChannelManager` starts and stops channel instances, each in its own thread. `_clear_singleton_cache` holds a map from type to class path. |
| `config.py` | `available_setting` dict = default config and allowed keys. Env vars whose lowercase name matches a key override it (`ast.literal_eval`). |
| `common/const.py` | Channel type constants (`TELEGRAM = "telegram"`, …). |
| `bridge/` | `Context` (type, content, kwargs), `Reply`/`ReplyType`, `Bridge` → agent/LLM. |
| `channel/` | One package per IM platform, plus the shared base classes. |
| `agent/tools/` | Agent tools (`BaseTool` subclasses), auto-registered from `agent/tools/__init__.py`. |
| `common/channel_registry.py` | Process-wide handle to the live `ChannelManager` (`get_channel_manager()`). |
| `tests/` | Flat pytest suite. `conftest.py` isolates HOME and the data dir. CI runs `pytest tests/ -q` and `ruff` (F-rules gated). |

## 2. Channel class hierarchy

```
Channel (channel/channel.py)
  ├─ cfg(key)                 per-instance credential override → conf()
  ├─ apply_instance(...)      multi-instance identity/creds/team
  ├─ stamp_instance_context() bound_agent_id / instance_id onto Context
  ├─ startup() / stop()       lifecycle (startup blocks)
  ├─ report_startup_success/error, wait_startup
  └─ send(reply, context)     abstract
ChatChannel (channel/chat_channel.py)  — starts consume() thread in __init__
  ├─ _compose_context(ctype, content, **kw)  whitelist/prefix/session logic
  ├─ produce(context)        enqueue per session_id
  ├─ consume()               worker pool → _handle → _generate_reply → _decorate_reply → _send_reply
  └─ _send(reply, ctx)       calls send(); on exception retries the WHOLE reply up to 2x
```

**Implication for `send()`**: raise only if *nothing* was delivered. A partial multi-chunk send must not raise, or the user gets duplicates (same contract as Slack/Discord; see `tests/test_slack_discord_send_retry.py`).

## 3. Inbound pattern (Telegram/Slack as reference)
1. Platform event → dedup (`ExpiredDict`) → group trigger gate.
2. Build a platform `ChatMessage` subclass (`msg_id, ctype, content, from_user_id, other_user_id, is_group, actual_user_id, is_at`).
3. Media: standalone image/file → `channel.file_cache.get_file_cache().add(session_id, path, type)`. The next text gets `[image: path]` refs appended. Media with a caption → merged text.
4. `context = self._compose_context(ctype, content, isgroup=..., msg=...)`, then set `context["session_id"]` and `context["receiver"]` plus platform keys, `stamp_speaker_from_channel(self, context, content)`, `self.produce(context)`.
5. IM channels neutralise the legacy WeChat gating in `__init__`: `conf()["group_name_white_list"] = ["ALL_GROUP"]`, `conf()["single_chat_prefix"] = [""]`, and handle triggering themselves.

## 4. Webhook-server pattern (wechat_kf / wechatcom)
- `web.py` application on a dedicated port (`web.httpserver.WSGIServer`). `startup()` blocks in `server.start()`, `stop()` calls `server.stop()`.
- The handler class is resolved by **module path string** (`"channel.x.x_channel.Query"`) and fetches the singleton channel. That breaks multi-instance. For WhatsApp we will instead bind the handler to the instance through a closure/class factory, so several instances (different ports) can coexist.
- Callbacks are acked fast and processing goes to a `ThreadPoolExecutor`.

## 5. Registration points for a new channel type

| File | What to add |
|---|---|
| `common/const.py` | `WHATSAPP = "whatsapp"` |
| `channel/channel_factory.py` | `elif` branch (with `_fresh` for multi-instance) |
| `app.py` `_clear_singleton_cache` | class path |
| `channel/channel_instances.py` | `CREDENTIAL_KEYS`, `_CHANNEL_TYPE_LABELS`, `MULTI_INSTANCE_READY` |
| `config.py` | default keys, and the `channel_type` comment's option list |
| `channel/web/api/channels.py` | `CHANNEL_DEFS` entry (console form), `EN_FIRST_CHANNELS` |
| `channel/web/core/channel.py` | startup banner channel lists |
| `docs/channels/` + `docs/docs.json` | user docs page |

## 6. Agent tools
- `BaseTool`: `name`, `description`, `params` (JSON schema), `execute(args) -> ToolResult`, optional `is_available()`.
- Registered by import in `agent/tools/__init__.py` (optional tools are wrapped in try/except inside `_import_optional_tools`). The `ToolManager` instantiates every class exported there.
- A tool reaches a running channel through `common.channel_registry.get_channel_manager().find_channels_by_type(type)`, the same approach the scheduler uses.

## 7. Reusable helpers
- `common.media_download.download_to_file(url, path, max_bytes, headers=...)`: streamed, size-capped.
- `channel.chat_message.safe_filename`, `common.state_dir.tmp_dir()`.
- `common.utils.split_string_by_utf8_length`, `scrub_secrets`.
- `common.expired_dict.ExpiredDict` for dedup.
- `common.singleton.singleton` exposes `__wrapped__` / `new_instance` (tests build instances with `Cls.__wrapped__.__new__`).

## 8. Testing conventions
- Plain pytest functions, `SimpleNamespace` fakes, monkeypatch. No network.
- Channel instances are built while bypassing `__init__` where convenient, or with the real constructor when the consume thread is harmless (daemon).
