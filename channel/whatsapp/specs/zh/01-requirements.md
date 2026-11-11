# WhatsApp 通道 — 需求规格

[English](../01-requirements.md) | 中文

状态：已评审（rev 2）。§5 列出了被修改或删除的原始需求。

## 1. 目标

新增 `whatsapp` 通道，让 CowAgent 的 Agent 以 WhatsApp 企业号码的身份工作：
接收消息、回复消息、创建和管理 WhatsApp 群组。集成**只**通过 Meta 官方的
**WhatsApp Business Cloud API**（Graph API）完成。不使用浏览器自动化、不使用 WhatsApp Web 协议，也不使用非官方库。

## 2. 前置条件（运维配置，不在代码范围内）

| # | 前置条件 |
|---|---|
| P1 | 一个 Meta Business portfolio、一个添加了 WhatsApp 产品的 Meta 开发者应用，以及一个 WhatsApp Business Account（WABA）。 |
| P2 | 手机号码已注册到 Cloud API，其 **WhatsApp Business 资料**（显示名称、简介等）已设置并通过审核。 |
| P3 | 一个永久的 **System User access token**，具有 `whatsapp_business_messaging` 权限（使用群组还需 `whatsapp_business_management`）。 |
| P4 | 一个转发到通道本地 Webhook 端口的**公网 HTTPS 地址**（反向代理或隧道）。Meta 只调用持有有效证书的 HTTPS 地址。 |
| P5 | Webhook 已订阅 `messages` 字段；如使用群组，还需订阅 `group_*` 字段。 |
| P6（仅群组） | 企业是 **Official Business Account（OBA）**。否则 Meta 返回资格（eligibility）错误。 |

## 3. 功能需求

### FR-1 配置
- 新增配置项（均以 `whatsapp_` 为前缀）：`whatsapp_access_token`、`whatsapp_phone_number_id`、
  `whatsapp_app_secret`、`whatsapp_verify_token`、`whatsapp_port`（默认 9893）、
  `whatsapp_api_version`（默认 `v21.0`），以及 FR-6 中的限速配置项。
- 密钥可通过环境变量覆盖，沿用现有的配置环境变量覆盖机制。
- 可通过 `channel_type: "whatsapp"` 选择该通道，并出现在 Web 控制台的通道列表中。与其他占用 Webhook 端口的通道一样，它是单实例的（见 03 §9 B1）。
- 缺少必填配置项时，启动立即失败并给出清晰错误。

### FR-2 Webhook 端点
- `GET /whatsapp/webhook`：Meta 验证握手。当 `hub.mode == subscribe` 且 `hub.verify_token` 匹配时回显 `hub.challenge`，否则返回 403。
- `POST /whatsapp/webhook`：校验 `X-Hub-Signature-256`（以 app secret 为密钥，对原始请求体做 HMAC-SHA256），不匹配返回 403。校验 payload 结构（`object == whatsapp_business_account`、`entry[].changes[]`），立即应答 `200`，再异步处理。
- 按 WhatsApp 消息 id 去重（Meta 会重复投递）。
- 忽略属于其他 `phone_number_id` 的事件（一个应用可以挂多个号码）。

### FR-3 接收消息
- 单聊和群聊中 `text`、`image`、`audio`（语音）、`document`、`video` 类型的入站消息转成 `ChatMessage`，进入标准的 `ChatChannel.produce()` 流程。
- 媒体文件通过 `GET /{media-id}` 获取媒体 URL 后下载（Bearer token），限制大小，以安全文件名保存到工作区 tmp 目录。
- 图片和文件沿用 Telegram 的文件缓存约定：单独发送的媒体消息先缓存，附加到下一条文本消息上；带说明文字的媒体作为一个请求处理。
- 群消息：`messages[].group_id` 标识群，`from` / `from_user_id` 标识发送者。
- 不支持的类型（贴纸、表情回应、位置、交互消息等）以 debug 级别记录后跳过。
- 记录 `statuses` Webhook（sent / delivered / read / failed）。`failed` 状态以 warning 级别记录错误码和标题。

### FR-4 发送消息
- 回复类型：`TEXT`/`ERROR`/`INFO`（按 4096 字符上限拆分）、`IMAGE_URL`/`IMAGE`、`FILE`、`VIDEO`/`VIDEO_URL`、`VOICE`（以音频发送）。
- 媒体先上传到 `POST /{phone-number-id}/media`，再按 media id 发送。
- 单聊使用 `recipient_type: individual`，`to` = wa_id。群聊使用 `recipient_type: group`，`to` = group_id。
- 主动发送（定时任务）走同一个 `send()`，受 24 小时规则约束（§4）。

### FR-5 群组管理（Groups API）— 以工具形式提供给 Agent
仅在 WhatsApp 通道运行时可用的 `whatsapp_group` Agent 工具，支持：
`create`（subject、description、join_approval_mode）· `list` · `info` · `update`（subject/description）·
`get_invite_link` · `reset_invite_link` · `remove_participants` · `list_join_requests` ·
`approve_join_requests` · `reject_join_requests` · `delete`。
- `create` 之后，邀请链接通过 `group_lifecycle_update` Webhook 异步送达。Meta 返回群 id 时工具会把它返回，Agent 之后可以调用 `get_invite_link`。
- `group_*` Webhook（生命周期、成员、设置）只记录日志，不转成聊天消息。
- Meta 的错误（code、title、message）原样返回给 Agent，让它能向用户解释，例如"不是 OBA"。

### FR-6 防封 / 限速
- **全局吞吐量**：每个号码一个令牌桶，`whatsapp_rate_limit_mps`（默认 20，Meta 硬上限 80）。
- **单收件人节奏**：每个收件人（用户或群）一个令牌桶，每 `whatsapp_pair_interval_seconds`（默认 6）秒补充 1 个令牌，突发上限 `whatsapp_pair_burst`（默认 5）。这对应 Meta 对同一用户约每 6 秒 1 条的 pair rate limit，并允许短时突发。
- 发送方**阻塞等待**令牌，消息永不丢弃。
- 对限流和临时错误进行**指数退避加抖动重试**，最多 3 次：`130429`（吞吐量）、`131056`（pair rate；同时清空该收件人的令牌桶，下次尝试等待一个完整间隔）、`80007`（WABA 速率）、`4`（应用速率）、`2`（临时错误），以及 HTTP 429/5xx/网络错误。单次发送在限速器上最多阻塞 `whatsapp_max_wait_seconds`（默认 120）秒。
- `131048`（垃圾消息限制，属于质量信号）和 `131047`（24 小时窗口已过期）**不重试**，以 error 级别记录并给出处理建议。
- 入站消息不加人为延迟。Webhook 处理先应答、后异步处理，Meta 不会遇到超时。

## 4. 运维必须了解的平台限制（写入文档，不做绕过）
- **24 小时客服窗口**：只能在用户最后一条消息后 24 小时内发送自由格式消息。超出后 Meta 要求使用已审核模板，本通道不发送模板（记录错误 `131047`）。
- **Groups API**：仅限 OBA，每群最多 **8 名成员**，成员**只能通过邀请链接**加入（没有添加成员的接口），WhatsApp Business *App* 共存（coexistence）号码不可用。
- Cloud API 上的**封号风险**主要来自**质量评级**（用户屏蔽或举报）和违反政策，而不只是发送速度。限速能减少限流错误和类似垃圾消息的突发发送，但无法保证账号永不受限。

## 5. 评审：修改 / 删除的需求

| 原始需求 | 决定 | 原因 |
|---|---|---|
| "接管人工注册的账号并冒充其身份" | **修改**：Agent 以*企业号码*身份、使用其自己已审核的 Business 资料工作。 | Cloud API 号码是企业身份，消息显示企业显示名称。把账号伪装成个人违反 WhatsApp Business 政策，Cloud API 在技术上也做不到。 |
| 同时继续在个人版 WhatsApp App 中使用该号码 | **删除** | 号码注册到 Cloud API 后就会从个人版 App 下线。 |
| "创建并管理群组"（自由添加成员） | **修改**为 Groups API 支持的能力：创建、邀请链接、入群申请审批、移除成员、修改、删除。 | 没有添加成员的接口，每群上限 8 人，且需要 OBA。 |
| "永不被封" | **修改**为"保持在 Meta 公布的限额内，并在出错时安全失败"（FR-6）。 | 没有任何客户端机制能做出这种保证（§4）。 |
| 避免 Chrome/网页自动化 | **保留**，Cloud API 的设计本身就满足。 | — |
| 发送模板 / 营销群发 | **不在范围内** | 需求未提出，属于新功能（约束 1）。 |
| 上传群头像、置顶消息、已读回执、输入中提示 | **不在范围内** | "收发/管理"不需要这些，保持改动最小。 |

## 6. 非功能需求
- **NFR-1 隔离**：不改变现有通道的行为。`channel/whatsapp/` 之外的修改仅限于注册点（工厂、常量、配置默认值、通道列表、工具注册）。
- **NFR-2 安全**：签名校验、常数时间比较、日志中不出现密钥（`scrub_secrets`）、下载限制大小、文件名安全化。
- **NFR-3 可测试性**：HTTP 统一走一个可注入的客户端，测试离线运行（不调用 Meta）。
- **NFR-4 可靠性**：Webhook 处理器绝不向服务器抛异常。错误连同上下文一起记录（不静默吞错）。
- **NFR-5 兼容性**：Python ≥3.9，只使用现有依赖（`requests`、`web.py`）。

## 7. 验收标准
1. 单元测试覆盖客户端、限速器、Webhook 校验与解析、消息映射、发送分发、群组服务和工具。
2. 集成测试端到端驱动一个真实的本地 Webhook HTTP 服务器：签名 POST → `produce()` → `send()` → 模拟的 Graph API，并覆盖签名拒绝和验证握手。
3. 现有完整测试套件仍全部通过，`ruff` 对新文件不报 F 类错误。
