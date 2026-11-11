# WhatsApp 通道 — 手工测试计划

[English](../04-manual-test-plan.md) | 中文

目标：针对**真实**的 Meta Cloud API 端到端验证通道，作为自动化单元测试和集成测试（`tests/test_whatsapp_*.py`，离线运行，使用假 Graph API）的补充。

配置步骤见用户手册：`docs/zh/channels/whatsapp.mdx`。每个用例列出**步骤 → 预期结果 → 证据**（手机上和/或 CowAgent 日志中应看到的内容）。在汇总表（§9）最后一列记录通过/失败。

## 0. 测试环境

| 项目 | 值 |
|---|---|
| Meta 应用 | 按手册 §三 创建 |
| 号码 | 阶段 A：Meta **测试号码**。阶段 B（可选）：你注册的正式号码。阶段 C（群组）：**OBA** 号码 |
| 测试手机 | **手机 1** = 你的 WhatsApp（已加入测试收件人）。**手机 2** = 另一个 WhatsApp 账号（用于群组和多用户测试） |
| 隧道 | `cloudflared tunnel --url http://localhost:9893`（或 ngrok） |
| CowAgent | 分支 `feat/whatsapp-channel`，`channel_type: "whatsapp"`，`debug: true`（用于查看状态日志） |
| 日志 | 在终端运行或跟踪日志文件；grep `[WhatsApp]` |

安全类测试需要先导出以下辅助变量和函数：

```bash
export WA_URL="https://<tunnel-host>/whatsapp/webhook"
export WA_SECRET="<whatsapp_app_secret>"
sign() { printf '%s' "$1" | openssl dgst -sha256 -hmac "$WA_SECRET" | sed 's/^.* //'; }
```

---

## 1. 启动和配置

| ID | 步骤 | 预期 | 证据 |
|---|---|---|---|
| S1 | 配齐 4 个必填项后启动 | 启动成功 | 日志：`✅ webhook listening on http://0.0.0.0:9893/whatsapp/webhook (phone_number_id=…)` |
| S2 | 删除 `whatsapp_app_secret` 后启动 | 启动失败并给出清晰错误；其他通道不受影响 | 日志：`missing required config: whatsapp_app_secret` |
| S3 | 占用端口（`python3 -m http.server 9893`）后启动 | 启动失败（不会误报"就绪"） | 日志：`startup failed: … address already in use` |
| S4 | Web 控制台 → 通道 → 添加 WhatsApp，填入凭证 | 通道卡片显示已连接；保存后密钥被遮盖 | 控制台界面 + S1 的日志行 |
| S5 | 密钥只放在环境变量中（`WHATSAPP_ACCESS_TOKEN` 等），config.json 中不放 | 与 S1 一样启动成功 | 日志：`override config by environ args`（值已遮盖） |

## 2. Webhook 验证和安全

| ID | 步骤 | 预期 | 证据 |
|---|---|---|---|
| W1 | Meta → Configuration → Webhook → Verify and save | Meta 接受 | Meta 界面显示绿色对勾 |
| W2 | `curl "$WA_URL?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=42"` | 响应体 `42`，HTTP 200 | curl 输出 |
| W3 | 与 W2 相同但 token 错误 | HTTP 403 | `curl -i` + 日志 `webhook verification refused` |
| W4 | 未签名的 POST：`curl -i -X POST "$WA_URL" -d '{"object":"whatsapp_business_account","entry":[]}'` | HTTP 403，不做任何处理 | 日志：`bad or missing X-Hub-Signature-256` |
| W5 | 签名错误的 POST：加上 `-H "X-Hub-Signature-256: sha256=00"` | HTTP 403 | 同 W4 |
| W6 | 签名正确但不是 WhatsApp 的请求体：`B='{"object":"page"}'; curl -i -X POST "$WA_URL" -H "X-Hub-Signature-256: sha256=$(sign "$B")" -d "$B"` | HTTP 200（避免 Meta 重试），被忽略 | 日志：`signed webhook payload rejected` |
| W7 | 超大请求体：`head -c 5000000 /dev/zero \| curl -i -X POST "$WA_URL" --data-binary @-` | HTTP 413，不崩溃 | curl 输出 |
| W8 | 浏览器访问 `https://<tunnel-host>/static/` 或 `/` | 404，没有文件列表，没有 traceback 页面 | 浏览器 |

## 3. 单聊：入站（手机 1 → 机器人）

前提：手机 1 在过去 24 小时内给号码发过消息（先回复 hello_world 模板）。

| ID | 步骤 | 预期 | 证据 |
|---|---|---|---|
| P1 | 发送 `hello` | Agent 在同一会话中回复 | 手机 1；日志 `sent 1 part(s) to individual <wa_id>` |
| P2 | 要求写长内容（"写一篇 1500 字关于茶的文章"） | 回答分成多条消息到达，顺序正确，没有丢失 | 手机 1；日志 `sent N part(s)`，N>1 |
| P3 | 发送一张**带说明文字** "图片里有什么？" 的照片 | 收到一条描述照片的回答 | 手机 1 |
| P4 | 发送一张**不带说明文字**的照片，再发文字 "描述一下" | 照片本身没有回复；文字得到关于照片的回答 | 日志 `image cached for session …`，然后是回答 |
| P5 | 发送一个 PDF，说明文字为 "总结一下" | 收到 PDF 摘要 | 手机 1 |
| P6 | 发送语音 "现在几点"（已配置语音识别服务） | 语音问题得到回答 | 手机 1 |
| P7 | 发送一个贴纸，再发送一个位置 | 不回复，也不报错 | 日志（debug）：`unsupported message type 'sticker' skipped` |
| P8 | 长任务运行时发送 `/cancel` | 任务停止，收到确认消息 | 手机 1 |
| P9 | 手机 1 和手机 2（都是测试收件人）同时聊天 | 各自收到自己的回答，上下文不串 | 两部手机 |
| P10 | 在两条消息之间重启 CowAgent | 重启后第二条消息仍被回答 | 手机 1 |

## 4. 单聊：出站媒体（机器人 → 手机 1）

| ID | 步骤 | 预期 | 证据 |
|---|---|---|---|
| O1 | 让 Agent 生成并发送一张图片（或发送本地 PNG/JPG） | 以照片形式到达 | 手机 1 |
| O2 | 让 Agent 写一份 markdown 报告并以文件发送 | `.md` 以带文件名的文档形式到达 | 手机 1 |
| O3 | 要一个 PDF 或 Excel 文件 | 以文档形式到达 | 手机 1 |
| O4 | 让它发送一个 GIF 或 ZIP 文件 | 不发送；你收到 "WhatsApp can't deliver this file type: <name>" | 手机 1；日志警告 `is a type WhatsApp does not accept` |
| O5 | 开启 `always_reply_voice: true` 后提问 | 收到语音/音频回复（mp3/ogg TTS），如果 TTS 生成的是 WAV 则收到提示 | 手机 1 |
| O6 | 让它发送一个公网 URL 上的图片 | 图片到达 | 手机 1 |
| O7 | 用 `WEB_SECURITY_SSRF_PROTECTION=true` 启动 CowAgent（项目级防护**默认关闭**），然后让它把 `http://127.0.0.1:…` 或 `http://169.254.169.254/…` 作为图片发送 | 不抓取也不发送；记录错误；不崩溃 | 日志 `cannot prepare … reply: ValueError` |

## 5. 限速和防封

为便于观察，临时设置 `whatsapp_pair_burst: 2`、`whatsapp_pair_interval_seconds: 10`。

| ID | 步骤 | 预期 | 证据 |
|---|---|---|---|
| R1 | 要求一个会拆成 ≥4 段的长回答（同 P2） | 前 2 段立即到达；后面每段间隔约 10 秒。没有丢失 | 手机上的时间戳 |
| R2 | R1 进行中，手机 2 发一条消息 | 手机 2 得到回复，无需等待手机 1 的节奏（桶相互独立） | 手机 2 的时间戳 |
| R3 | 设置 `whatsapp_max_wait_seconds: 5`，重复 R1 | 需要等待超过 5 秒的分段被放弃，而不是一直阻塞 | 日志 `abandoned after N part(s)` |
| R4 | 在整个测试日中查看 WhatsApp Manager → 质量评级 | 保持绿色；日志中没有 `131048` | WhatsApp Manager，`grep 131048` |

测试完成后恢复默认值。

## 6. 24 小时窗口

| ID | 步骤 | 预期 | 证据 |
|---|---|---|---|
| T1 | 在窗口内，让 Agent 安排 2 分钟后给手机 1 发一条消息 | 消息按时到达 | 手机 1 |
| T2 | 给过去 24 小时内**没有**发过消息的收件人安排一条消息（或等待超过 24 小时） | 不送达；日志说明清楚；不重试 | 日志 `24-hour customer service window is closed (131047)`，只出现一次 |

## 7. 群组（阶段 C：需要 Official Business Account）

先订阅 `group_*` Webhook 字段（手册 §6.2）。

| ID | 步骤 | 预期 | 证据 |
|---|---|---|---|
| G1 | 在**非 OBA** 号码上："创建一个叫 Test 的 WhatsApp 群" | Agent 报告 Meta 的资格错误（不崩溃） | Agent 回复中引用了该错误 |
| G2 | 在 OBA 号码上："创建一个叫 Launch crew 的 WhatsApp 群，入群需要审批" | 群创建成功 | Agent 回复；日志 `group event group_lifecycle_update: groups=['…']`（日志中没有邀请链接） |
| G3 | "列出我的 WhatsApp 群" | 显示 Launch crew 及其 id | Agent 回复 |
| G4 | "给我 Launch crew 的邀请链接" | `https://chat.whatsapp.com/…` 链接 | Agent 回复 |
| G5 | 手机 2 打开链接申请入群；问 "谁在申请入群？" 再说 "都通过" | 列出入群申请，然后通过；手机 2 进入群 | Agent 回复；手机 2 |
| G6 | 手机 2 在群里发 "hi bot" | Agent **在群内**回复，没有字面的 "@name" 前缀 | 群聊；日志 `sent … to group <id>` |
| G7 | 设置 `whatsapp_group_trigger: "prefix"`；手机 2 先发 "hello" 再发 "@bot hello" | 只有第二条被回复 | 群聊 |
| G8 | "把群改名为 Launch team，并把描述设为 …" | 手机上群名和描述发生变化 | 手机 |
| G9 | "重置邀请链接"；用第三部手机尝试旧链接 | 旧链接失效 | 手机 3 |
| G10 | "把 <手机 2 号码> 移出群" | 手机 2 被移出（且无法再通过链接加入） | 手机 2 |
| G11 | 让 Agent 删除群 id `me`，或你的 WABA id | 被拒绝（"not a WhatsApp group"）；没有删除任何东西 | Agent 回复 |
| G12 | "删除群 Launch team" | 群被删除 | 手机；日志中的群组事件 |
| G13 | 停止 WhatsApp 通道，然后（通过 Web）让 Agent 列出 WhatsApp 群 | Agent 没有 WhatsApp 群组工具 / 说明通道未运行 | Agent 回复 |

## 8. 回归：其他通道

| ID | 步骤 | 预期 |
|---|---|---|
| X1 | 运行 `channel_type: "web,whatsapp"`；在 Web 上聊天 | Web 工作正常（WhatsApp 不修改全局触发配置） |
| X2 | 同时运行另一个 IM 通道（如 Telegram）和 WhatsApp | 两者各自独立回复 |

## 9. 结果

| 范围 | ID | 通过/失败 | 备注 |
|---|---|---|---|
| 启动 | S1–S5 | | |
| Webhook 安全 | W1–W8 | | |
| 单聊入站 | P1–P10 | | |
| 出站媒体 | O1–O7 | | |
| 限速 | R1–R4 | | |
| 24 小时窗口 | T1–T2 | | |
| 群组 | G1–G13 | | |
| 回归 | X1–X2 | | |

**退出标准**：所有 S、W、P、O、R、T 和 X 测试在测试号码上通过。G 测试在 OBA 号码上通过；如果没有 OBA 号码，则 G1 通过，G2–G13 标记为 N/A。
