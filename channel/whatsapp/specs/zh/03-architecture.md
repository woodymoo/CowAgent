# WhatsApp 通道 — 架构设计

[English](../03-architecture.md) | 中文

输入：`01-requirements.md`、`02-codebase-analysis.md`。

## 1. 遵循的原则
- **单一职责**：传输、策略（限速/重试）、协议解析、领域映射、群组操作和生命周期各自放在独立模块中。
- **依赖倒置 / 可测试性**：所有做 I/O 的模块都通过注入获取协作者（HTTP session、时钟、sleep），测试从不访问网络，也不真正 sleep。
- **开闭原则**：只在现有框架的注册点上扩展，不修改基类。
- **快速失败，绝不静默**：配置错误通过 `report_startup_error` 让 `startup()` 失败。每个 Webhook payload 错误都连同上下文记录，绝不让服务器崩溃。

## 2. 模块布局

```
channel/whatsapp/
├── __init__.py
├── whatsapp_api.py          # WhatsAppApiClient + WhatsAppApiError  （只负责传输）
├── whatsapp_rate_limiter.py # TokenBucket, SendThrottle             （纯策略，时钟可注入）
├── whatsapp_sender.py       # WhatsAppSender：限速 → 客户端 → 重试/退避
├── whatsapp_webhook.py      # verify_signature, verify_subscription, parse_webhook → dataclass
├── whatsapp_server.py       # 绑定到【一个】通道实例的 web.py 应用工厂
├── whatsapp_message.py      # 由 InboundMessage 构造 WhatsAppMessage(ChatMessage)
├── whatsapp_groups.py       # WhatsAppGroupService（Groups API）
└── whatsapp_channel.py      # WhatsAppChannel(ChatChannel)：生命周期、入站、send()
agent/tools/whatsapp_group/
├── __init__.py
└── whatsapp_group.py        # WhatsAppGroupTool → 运行中通道的 group_service
```

依赖方向（无环）：

```
whatsapp_channel ─┬─> whatsapp_server ─> whatsapp_webhook
                  ├─> whatsapp_message ─> whatsapp_webhook (InboundMessage)
                  ├─> whatsapp_sender ──> whatsapp_api, whatsapp_rate_limiter
                  └─> whatsapp_groups ──> whatsapp_api
agent/tools/whatsapp_group ─> common.channel_registry（运行时查找）─> channel.group_service
```

## 3. 组件

### 3.1 `WhatsAppApiClient`（whatsapp_api.py）
```python
class WhatsAppApiError(Exception):
    http_status: int; code: Optional[int]; title: str; message: str; details: str
    retryable: bool  # property：HTTP 429/5xx，或 code 属于 {130429, 131056, 80007, 4, 2}
class WhatsAppApiClient:
    def __init__(self, access_token, phone_number_id, api_version="v21.0",
                 session=None, base_url="https://graph.facebook.com", timeout=(5, 30))
    def request(self, method, path, *, json=None, params=None, data=None, files=None) -> dict
    def send_message(self, payload: dict) -> dict          # POST /{pnid}/messages
    def upload_media(self, file_path, mime_type) -> str    # POST /{pnid}/media → id
    def get_media_url(self, media_id) -> str               # GET /{media_id}
    def download_media(self, url, dest_path, max_bytes) -> None  # 通过 common.media_download 以 Bearer 方式 GET
```
- 把 Graph 错误信封（`{"error": {code, message, error_user_title, error_data.details}}`）解析为 `WhatsAppApiError`。
- 绝不记录 token。错误文本经过 `scrub_secrets` 处理。

### 3.2 限速（whatsapp_rate_limiter.py）
```python
class TokenBucket:   # capacity, refill_per_sec, clock=time.monotonic
    def try_acquire(self) -> float   # 拿到令牌返回 0.0，否则返回需等待的秒数（不扣令牌）
class SendThrottle:
    def __init__(self, global_rate, pair_interval, pair_burst, clock=..., sleep=..., max_pairs=10_000)
    def acquire(self, recipient: str) -> float  # 阻塞直到两个桶都放行；返回总等待时间
```
- 全局桶：容量 = `global_rate`，补充速率 = `global_rate`/秒。
- 每个收件人一个 pair 桶：容量 = `pair_burst`，补充速率 = 1/`pair_interval`。pair 桶存放在上限为 `max_pairs` 的 `OrderedDict` 中，按 LRU 淘汰。
- 获取顺序：先 pair 后全局，这样慢的 pair 在 sleep 时不会占着全局令牌。一把锁保护桶的计算，**sleep 在锁外进行**。

### 3.3 `WhatsAppSender`（whatsapp_sender.py）
```python
class WhatsAppSender:
    def __init__(self, client, throttle, max_attempts=3, base_delay=1.0, sleep=time.sleep, rand=random.random)
    def send(self, recipient, recipient_type, msg_type, body: dict) -> dict
```
- 构造 `{"messaging_product":"whatsapp","recipient_type":…, "to":…, "type":msg_type, msg_type: body}`。
- 循环：`throttle.acquire(to)` → `client.send_message()`。遇到 `WhatsAppApiError.retryable` 时按 `base_delay * 2**attempt * (0.5 + rand())` 退避后重试。不可重试的错误立即重新抛出。`131048` 和 `131047` 以 error 级别记录，并附带给运维的提示。

### 3.4 Webhook 协议（whatsapp_webhook.py）
纯函数和冻结的 dataclass，不依赖 web.py，也不依赖通道：
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
- 在边界做结构校验：`object` 必须是 `whatsapp_business_account`，`entry`/`changes` 必须是列表。格式错误的元素被跳过并记录，不会让整批失败。
- `metadata.phone_number_id` 与本号码不同的 `messages` 变更被忽略。
- `sender_id = from or from_user_id`。`sender_name` 取自 `contacts[]` 中按 `wa_id`/`user_id` 匹配到的条目。

### 3.5 HTTP 服务器（whatsapp_server.py）
```python
def build_wsgi_app(on_verify, on_event) -> web.application
class WebhookServer:  # 封装 web.httpserver.WSGIServer；start() 阻塞，stop()
```
- 路由 `/whatsapp/webhook` 映射到一个**按实例创建**的处理器类（对回调形成闭包）。这修正了 wechat_kf 模式中与单例的耦合，允许多实例使用不同端口。
- `POST`：读取原始请求体，调用 `on_event(raw, signature_header)`，它返回 HTTP 状态码（200/400/403）。异步分发由通道负责。

### 3.6 `WhatsAppMessage`（whatsapp_message.py）
把 `InboundMessage`（加上本地媒体路径）映射到 `ChatMessage` 字段：
- 单聊：`other_user_id = sender_id`。
- 群聊：`is_group=True`、`other_user_id = group_id`、`actual_user_id = sender_id`、`is_at = True`（是否触发在构造前已经判定）。

### 3.7 `WhatsAppGroupService`（whatsapp_groups.py）
每个 Groups API 端点对应一个方法（create、list、info、update、delete、获取/重置邀请链接、移除成员、列出/通过/拒绝入群申请）。调用前校验参数：subject 非空且 ≤128、description ≤2048、成员 1–8 个、join_approval_mode 属于允许集合。返回 Graph JSON，错误以 `WhatsAppApiError` 向上传播。

### 3.8 `WhatsAppChannel`（whatsapp_channel.py）
`@singleton class WhatsAppChannel(ChatChannel)`，`NOT_SUPPORT_REPLYTYPE = []`。
- `__init__`：去重 `ExpiredDict(3600)`、执行器（4 个 worker），以及与 Telegram 相同的 conf 中和（`group_name_white_list=["ALL_GROUP"]`、`single_chat_prefix=[""]`）。
- `startup()`：通过 `self.cfg` 读取凭证。token / phone_number_id / app_secret / verify_token 任一缺失 → `report_startup_error`。然后构造 client、throttle、sender 和 group_service，在 `whatsapp_port` 上启动 `WebhookServer`，调用 `report_startup_success()` 并阻塞。
- `stop()`：停止服务器并关闭执行器。
- `_on_verify(mode, token, challenge)` → `verify_subscription`。
- `_on_event(raw, sig)` → 签名错误返回 403；已签名但 JSON 或结构错误时记录日志并返回 200（M4）。否则把每条消息放进按会话划分的 FIFO 收件箱（键为群 id 或发送者 id），返回 200。每个会话同一时间只有一个排空任务在执行器上运行，因此同一会话的消息按到达顺序处理（文字不会抢在之前发的照片前面），不同会话仍然并行。状态和群组事件交给 `_process_batch`，只记录日志。
- `_process_batch`：记录 statuses（`failed` 用 warning）和群组事件，然后对每条消息调用 `_handle_inbound(m)`。
- `_handle_inbound`：去重 → 群聊触发（`whatsapp_group_trigger`：`all` | 使用 `group_chat_prefix` 的 `prefix`）→ 下载媒体 → 文件缓存约定 → `_compose_context` → 设置 `session_id`、`receiver`、`whatsapp_recipient_type`、`no_need_at=True`（字面上的 "@name" 不是 WhatsApp 的 @提及）→ `stamp_speaker_from_channel` → `produce`。
- `send(reply, context)`：解析 `(to, recipient_type)`（优先使用 context 中的字段；否则匹配手机号数字正则的是单聊，其余视为群，以覆盖定时任务发送），再按 `ReplyType` 分发：
  - 文本 → 按 4096 字符拆分 → `sender.send(..., "text", {"body": chunk})`
  - 图片 / 文件 / 视频 / 语音 → 解析为本地文件（`file://`、路径，或限制大小的 http 下载）→ `client.upload_media` → `sender.send(..., type, {"id": media_id, ...})`
  - **重试约定**：只有第一个单元失败（什么都没发出去）时才抛异常。之后的失败在*记录日志后*吞掉，与 ChatChannel `_send` 的语义一致。

### 3.9 `WhatsAppGroupTool`（agent/tools/whatsapp_group/whatsapp_group.py）
- `name = "whatsapp_group"`。`params` JSON schema 中 `action` 是枚举，外加各动作的字段。
- `is_available()`：仅当存在带 `group_service` 的运行中 WhatsApp 通道时为真（通过 `get_channel_manager().find_channels_by_type("whatsapp")` 查找）。如有多个实例，优先选择绑定到调用方 Agent 的实例（`instance_id` 参数可选）。
- `execute(args)` → 分发表 → `ToolResult.success(json)`。`WhatsAppApiError` 转为带 code、title 和 message 的 `ToolResult.fail`。

## 4. 关键流程

**入站**
```
Meta ──POST──> WebhookServer ─> channel._on_event(raw,sig)
   verify_signature ✗→403 │ parse_webhook ✗→200（记录日志） │ 按会话收件箱 + 执行器 → 200
_drain_inbox(会话) → _handle_inbound →（下载媒体）→ _compose_context → produce()
   → ChatChannel.consume → agent → _send_reply → channel.send()
```
**出站**
```
send() → [upload_media] → WhatsAppSender.send → SendThrottle.acquire(to) → client.send_message
      ↖ 重试（130429/131056/80007/429/5xx，≤3 次，指数退避 + 抖动）
```

## 5. 注册点修改（channel/whatsapp 之外）
`common/const.py`（WHATSAPP）、`channel/channel_factory.py`、`app.py::_clear_singleton_cache`、`channel/channel_instances.py`（CREDENTIAL_KEYS / labels / MULTI_INSTANCE_READY）、`config.py` 默认值和通道列表注释、`channel/web/api/channels.py`（CHANNEL_DEFS + EN_FIRST_CHANNELS）、`channel/web/core/channel.py`（横幅列表）、`agent/tools/__init__.py`（可选 import）、`docs/channels/whatsapp.mdx` + `docs/docs.json`。

## 6. 配置项
| 配置项 | 默认值 | 说明 |
|---|---|---|
| whatsapp_access_token | "" | 密钥，凭证 |
| whatsapp_phone_number_id | "" | 凭证 |
| whatsapp_app_secret | "" | 密钥，凭证（签名） |
| whatsapp_verify_token | "" | 密钥，凭证（握手） |
| whatsapp_port | 9893 | 本地 Webhook 端口 |
| whatsapp_api_version | "v21.0" | Graph 版本 |
| whatsapp_group_trigger | "all" | `all` \| `prefix` |
| whatsapp_rate_limit_mps | 20 | 全局每秒消息数（Meta 上限 80） |
| whatsapp_pair_interval_seconds | 6 | 单收件人补充间隔 |
| whatsapp_pair_burst | 5 | 单收件人突发上限 |

## 7. 测试策略（TDD 顺序）
| 层 | 测试文件 | 手段 |
|---|---|---|
| 限速器 | `tests/test_whatsapp_rate_limiter.py` | 假时钟和假 sleep |
| API 客户端 | `tests/test_whatsapp_api.py` | 假 `requests.Session` |
| 发送器 | `tests/test_whatsapp_sender.py` | 假客户端和假限速器，断言重试/不重试 |
| Webhook | `tests/test_whatsapp_webhook.py` | 签名向量、payload 样例 |
| 群组 | `tests/test_whatsapp_groups.py` | 假客户端、参数校验 |
| 通道 | `tests/test_whatsapp_channel.py` | 入站 → 捕获 produce、发送分发、重试约定 |
| 工具 | `tests/test_whatsapp_group_tool.py` | 假 channel manager |
| 注册 | `tests/test_whatsapp_registration.py` | factory/const/instances/config/控制台定义 |
| **集成** | `tests/test_whatsapp_integration.py` | 临时端口上的真实 `WebhookServer` + 本地假 Graph HTTP 服务器；签名 POST → 回复发到假 Graph |

## 8. 风险
- Groups API 中文档字段以外的 payload 结构可能变化。我们把原始 JSON 返回给 Agent，因此可以容忍。
- 群 id 格式没有文档说明。只有当 context 中缺少 `whatsapp_recipient_type` 时才使用数字启发式判断。
- web.py `WSGIServer` 按端口运行：两个实例必须使用不同的 `whatsapp_port`。端口不是凭证项；如果提供了按实例的 `cfg("whatsapp_port")` 覆盖，会被采用。

---

## 9. 架构评审 — 处理结果（rev 2，与 §1–8 不一致之处以本节为准）

由 backend-architect agent 对照实际代码评审，所有发现均被采纳：

| # | 发现 | 处理 |
|---|---|---|
| B1 | 按实例设置的 `whatsapp_port` 无法生效：只有 `CREDENTIAL_KEYS` 会按实例复制，两个实例会在同一端口冲突。 | WhatsApp 以**单实例**运行，与其他占用 Webhook 端口的通道（`wechat_kf`、`wechatcom_app`）一致。**不**加入 `channel_instances.py`。工厂直接构造普通单例。 |
| B2 | 启动在端口绑定之前就报告了成功。 | `server.prepare()`（cheroot 在这里绑定，失败会抛异常）→ 失败时 `report_startup_error` 并返回；否则 `report_startup_success()`，再 `server.serve()`。 |
| M1 | 发送器重试 × `ChatChannel._send` 重试会相乘（最多 9 次，且 131047/131048 会被重发）。 | `send()` 捕获 `WhatsAppApiError`。**不可重试**的错误以 error 级别记录后返回。只有当可重试错误耗尽**且尚未发出任何内容**时才抛异常。 |
| M2 | 1 秒退避比 6 秒的 pair 窗口短。 | 收到 131056 时发送器调用 `throttle.penalize(to)`，清空该收件人的 pair 桶，使下次 `acquire` 等待一个完整的 pair 间隔。 |
| M3 | 修改全局 `conf()`（Telegram 的做法）破坏隔离（NFR-1）。 | **不修改 conf。**像 Telegram 那样重写 `_compose_context`（设置 session_id/receiver，跳过旧版白名单、前缀和 @ 检查），保留 `no_need_at=True`。 |
| M4 | 对已签名但内容错误的 payload 返回 400，会让 Meta 连续重试数天。 | 只有签名错误或缺失时返回 403。已签名但 JSON 或结构错误的 payload 以 error 级别记录，返回 **200**。捕获执行器关闭后的 `executor.submit` 异常。 |
| M5 | 请求体在 HMAC 校验之前被无限制读取；另外 `StaticMiddleware` 会暴露 `./static`。 | `server.max_request_body_size = 4 MiB`（cheroot 返回 413）。不使用 `StaticMiddleware`。`autoreload=False`。处理器有兜底捕获，web.py 调试页永远不会渲染。 |
| M6 | 出站媒体 URL 来自模型（SSRF）。 | 与 Telegram 一样使用 `download_to_file(..., guarded=True, max_seconds=…)`。 |
| m | 限速器算法。 | 在锁内计算两个桶的等待时间，只有两者都为 0 时才同时扣令牌；否则在锁外 sleep max(wait) 后循环。`max_wait`（默认 120 秒）限制总阻塞时间：超过后限速器**抛出 `ThrottleTimeout`**（该消息不发送，并记录错误）。这保护了共享的 8 worker `handler_pool`。 |
| m | 收件人类型的启发式判断不可靠。 | 群收件人在 `context["receiver"]` 中编码为 **`group:<group_id>`**，类型可以在定时任务往返中保留。删除数字启发式判断。 |
| m | 可重试错误码列表。 | {130429, 131056, 80007, 4（应用速率限制）, 2（临时服务错误）} + HTTP 429/5xx/网络错误。FR-6 已同步更新。 |
| m | 去重竞态。 | 检查并设置由锁保护。 |
| m | 工具细节。 | `is_available()` 绝不抛异常。构造函数不做 I/O，通道相关 import 延迟加载。在 `__all__` 中导出。**没有 `instance_id` 参数**（单实例）。 |
| m | 媒体类型。 | 非 jpeg/png 的出站图片以 `document` 发送。 |

注册点修改（最终版）：`common/const.py`、`channel/channel_factory.py`、`app.py::_clear_singleton_cache`、`config.py`、`channel/web/api/channels.py`、`channel/web/core/channel.py`、`agent/tools/__init__.py`、`docs/channels/whatsapp.mdx`、`docs/docs.json`。

## 10. 代码评审 — 处理结果

实现完成后的独立代码评审（0 个严重、1 个高、3 个中、4 个低）。每条发现都对照代码核实，并以测试先行的方式修复：

| # | 发现 | 修复 |
|---|---|---|
| H1 | Meta 不接受的媒体格式（gif、wav、zip 等）被上传后因不可重试的 400 失败，用户什么也收不到。 | 按类型设置 MIME 白名单，取自 Meta 的支持媒体表。文本类文件（`.md`、`.csv`、`.json` 等）以 `text/plain` 文档发送。其他类型不上传，改为给用户发一条简短的文字提示。 |
| M1 | `POST /messages` 读超时会被重试，可能导致消息重复。 | 只有 `ConnectionError`/`ConnectTimeout`（请求根本没发出去）可重试，`ReadTimeout` 不可重试。 |
| M2 | `whatsapp_rate_limit_mps` < 1 会阻塞所有发送。 | 全局桶容量为 `max(1, rate)`。 |
| M3 | 通过字符检查的 `group_id` 可能是任意 Graph 节点（`me`、`.`、WABA id）。 | 拒绝保留节点和本号码的 phone_number_id。`delete`/`remove_participants` 先确认目标是群（`GET ?fields=subject`）。 |
| L1 | 非 ASCII 的签名或 verify token 会让 `compare_digest` 抛异常，导致 500 并记录 traceback。 | 以字节形式比较，干净地拒绝。 |
| L2 | 邀请链接以 INFO 级别记录到日志。 | 群组事件只记录字段名和群 id。 |
| L3 | 远程媒体在被 Meta 拒绝前最多下载 100 MB。 | 下载上限按 Meta 各类型限制设置（图片 5 MB、音视频 16 MB、文档 100 MB）。 |
| L4 | 规格文档放在运行时包内。 | 由维护者决定存放位置（见 PR 说明）。 |
