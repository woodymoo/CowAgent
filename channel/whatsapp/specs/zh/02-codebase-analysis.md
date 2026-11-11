# 代码库分析 — 通道子系统（与 WhatsApp 相关部分）

[English](../02-codebase-analysis.md) | 中文

## 1. 顶层结构

| 路径 | 作用 |
|---|---|
| `app.py` | 入口。`ChannelManager` 启动和停止通道实例，每个实例运行在自己的线程中。`_clear_singleton_cache` 保存类型到类路径的映射。 |
| `config.py` | `available_setting` 字典 = 默认配置和允许的配置项。小写名称与配置项匹配的环境变量会覆盖它（`ast.literal_eval`）。 |
| `common/const.py` | 通道类型常量（`TELEGRAM = "telegram"` 等）。 |
| `bridge/` | `Context`（type、content、kwargs）、`Reply`/`ReplyType`、`Bridge` → agent/LLM。 |
| `channel/` | 每个 IM 平台一个包，外加共享基类。 |
| `agent/tools/` | Agent 工具（`BaseTool` 子类），在 `agent/tools/__init__.py` 中自动注册。 |
| `common/channel_registry.py` | 进程级的运行中 `ChannelManager` 句柄（`get_channel_manager()`）。 |
| `tests/` | 扁平的 pytest 测试套件。`conftest.py` 隔离 HOME 和数据目录。CI 运行 `pytest tests/ -q` 和 `ruff`（F 类规则作为门禁）。 |

## 2. 通道类层次

```
Channel (channel/channel.py)
  ├─ cfg(key)                 按实例覆盖凭证 → conf()
  ├─ apply_instance(...)      多实例的身份/凭证/团队
  ├─ stamp_instance_context() 把 bound_agent_id / instance_id 写入 Context
  ├─ startup() / stop()       生命周期（startup 阻塞）
  ├─ report_startup_success/error, wait_startup
  └─ send(reply, context)     抽象方法
ChatChannel (channel/chat_channel.py)  — 在 __init__ 中启动 consume() 线程
  ├─ _compose_context(ctype, content, **kw)  白名单/前缀/会话逻辑
  ├─ produce(context)        按 session_id 入队
  ├─ consume()               工作线程池 → _handle → _generate_reply → _decorate_reply → _send_reply
  └─ _send(reply, ctx)       调用 send()；出现异常时把整条回复重试最多 2 次
```

**对 `send()` 的影响**：只有在*什么都没发出去*时才抛异常。多段发送中途失败不能抛异常，否则用户会收到重复消息（与 Slack/Discord 的约定一致；见 `tests/test_slack_discord_send_retry.py`）。

## 3. 入站模式（参考 Telegram/Slack）
1. 平台事件 → 去重（`ExpiredDict`）→ 群聊触发判断。
2. 构造平台专属的 `ChatMessage` 子类（`msg_id, ctype, content, from_user_id, other_user_id, is_group, actual_user_id, is_at`）。
3. 媒体：单独的图片/文件 → `channel.file_cache.get_file_cache().add(session_id, path, type)`。下一条文本会附加 `[image: path]` 引用。带说明文字的媒体 → 合并为文本。
4. `context = self._compose_context(ctype, content, isgroup=..., msg=...)`，再设置 `context["session_id"]`、`context["receiver"]` 和平台专属字段，调用 `stamp_speaker_from_channel(self, context, content)`、`self.produce(context)`。
5. IM 通道在 `__init__` 中关闭旧版微信的过滤逻辑：`conf()["group_name_white_list"] = ["ALL_GROUP"]`、`conf()["single_chat_prefix"] = [""]`，由通道自己处理触发。

## 4. Webhook 服务器模式（wechat_kf / wechatcom）
- 在独立端口上运行 `web.py` 应用（`web.httpserver.WSGIServer`）。`startup()` 阻塞在 `server.start()`，`stop()` 调用 `server.stop()`。
- 处理器类通过**模块路径字符串**（`"channel.x.x_channel.Query"`）解析，并获取单例通道，这会破坏多实例。WhatsApp 改为通过闭包/类工厂把处理器绑定到实例上，使多个实例（不同端口）可以共存。
- 回调快速应答，处理交给 `ThreadPoolExecutor`。

## 5. 新通道类型的注册点

| 文件 | 需要添加的内容 |
|---|---|
| `common/const.py` | `WHATSAPP = "whatsapp"` |
| `channel/channel_factory.py` | `elif` 分支（多实例时用 `_fresh`） |
| `app.py` `_clear_singleton_cache` | 类路径 |
| `channel/channel_instances.py` | `CREDENTIAL_KEYS`、`_CHANNEL_TYPE_LABELS`、`MULTI_INSTANCE_READY` |
| `config.py` | 默认配置项，以及 `channel_type` 注释中的选项列表 |
| `channel/web/api/channels.py` | `CHANNEL_DEFS` 条目（控制台表单）、`EN_FIRST_CHANNELS` |
| `channel/web/core/channel.py` | 启动横幅中的通道列表 |
| `docs/channels/` + `docs/docs.json` | 用户文档页 |

## 6. Agent 工具
- `BaseTool`：`name`、`description`、`params`（JSON schema）、`execute(args) -> ToolResult`，可选 `is_available()`。
- 通过在 `agent/tools/__init__.py` 中 import 注册（可选工具在 `_import_optional_tools` 里用 try/except 包裹）。`ToolManager` 会实例化其中导出的每个类。
- 工具通过 `common.channel_registry.get_channel_manager().find_channels_by_type(type)` 访问运行中的通道，与定时任务的做法相同。

## 7. 可复用的工具函数
- `common.media_download.download_to_file(url, path, max_bytes, headers=...)`：流式下载，限制大小。
- `channel.chat_message.safe_filename`、`common.state_dir.tmp_dir()`。
- `common.utils.split_string_by_utf8_length`、`scrub_secrets`。
- `common.expired_dict.ExpiredDict` 用于去重。
- `common.singleton.singleton` 暴露 `__wrapped__` / `new_instance`（测试中用 `Cls.__wrapped__.__new__` 构造实例）。

## 8. 测试约定
- 普通 pytest 函数、`SimpleNamespace` 假对象、monkeypatch。不访问网络。
- 构造通道实例时，方便的话绕过 `__init__`；如果 consume 线程无害（daemon），则使用真实构造函数。
