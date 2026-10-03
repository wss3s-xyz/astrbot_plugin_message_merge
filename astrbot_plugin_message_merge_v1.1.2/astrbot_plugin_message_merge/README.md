# 补话合并 · astrbot_plugin_message_merge

上半句先别急着发出去 —— 插件把它**扣住**，静默等 **6 秒**；这期间补的第二句、第三句会按序**合并成一条**再交给模型。不插提示词、不改上下文、不额外调模型，**零 token**。

```
你：帮我看看那个报错        ← 扣住，开始 6 秒静默计时
你：就是昨天那个 500        ← 重置计时，攒进缓冲
你：日志我发你              ← 重置计时，攒进缓冲
         （6 秒没人说话 → 放行）
模型真正收到的 prompt：「帮我看看那个报错 / 昨天那个 500 / 日志我发你」
                          ← 只有一次请求，一次计费
```

## 1. 安装

1. 整个目录放进 `AstrBot/data/plugins/`，**目录名必须是 `astrbot_plugin_message_merge`**；
2. Dashboard → 插件管理 → 点一次**重载插件**（不必重启整个 AstrBot）；
3. 依赖：AstrBot `>= 4.25.5`，无第三方包。

## 2. 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关，关掉后消息不再被扣住 |
| `wait_seconds` | `6` | 静默秒数，每来一条新消息重新计时；`0` = 不等，直接放行 |
| `max_wait_seconds` | `18` | 从第一条算起的硬上限，超时强行放行 |
| `max_messages` | `8` | 单轮最多合并条数（含第一条），攒够立刻放行 |
| `merge_separator` | `\n` | 连接符。写「反斜杠 + n」两个字符 = 换行（三条拼三行）；空格 = 连成一句；`\n\n` = 段间空一行 |
| `scope_mode` | `private_only` | 面板上是下拉三选：仅私聊 / 仅群聊 / 私聊 + 群聊 |
| `user_whitelist` | 空 | 生效的用户 ID，面板上逐条添加；留空 = 不限制 |
| `group_whitelist` | 空 | 生效的群号，同上；仅在群聊模式下有意义 |
| `chat_whitelist` | 空 | 精确到单个会话的 `unified_msg_origin`，同上 |
| `target_users` | 空 | 1.0.x 旧字段，仍生效，等价于 `user_whitelist`（面板已隐藏） |
| `debug` | `false` | 打印扣住的每条消息与合并结果，排查完记得关 |

白名单是「与」关系：先过 `scope_mode`，再依次过 `chat_whitelist` → `group_whitelist` → `user_whitelist`。

## 3. 开群聊之前（重要）

默认 `private_only`，群聊完全不参与 —— 装上去和没装一样。

改成 `group_only` / `all` 后，**请务必同时填 `group_whitelist`**：框架 `WakingCheckStage` 只要看到有 filter 通过就会把 `is_wake` 置 True，所以**名单内的群里，机器人会对每条消息都进入请求流程**（适合「这个群就是让 bot 全自动接话」）；名单外的群完全不受影响，未命中的消息在 filter 层就被否掉，`is_wake` 保持原样。

一句话：要群聊，就把群号写进 `group_whitelist`，一次填准。

## 4. 原理

两条钩子，一条扣、一条等：

- **`hold_follow_up`**：`event_message_type(GROUP|PRIVATE, priority=100000)` + `custom_filter(ScopeFilter)`。会话已有等待者就把这条塞进缓冲并 `stop_event()` —— 框架认为「这条已有结果」，模型链路整段跳过，不花 token。
- **`wait_then_merge`**：`on_waiting_llm_request(priority=100000)`。事件走到「马上要请求模型」时进来静默睡 `wait_seconds`，静默到点 / 攒够 / 超时就合并。等待点在 `session_lock_manager.acquire_lock` 之前触发，所以**不占会话锁、不卡别的会话**。
- **`_merge`**：文本按 `merge_separator` 拼回 `event.message_str`，非文本组件（图 / 音 / 文件）按序补回，之后记忆、人设、分段、表情全部照旧。

踩过的坑：

- 纯空白且无组件的消息直接放行；`/` 开头的指令不等待；
- 优先级取 `100000`，排在 `meme_manager`（`99999`）之前，免得它 `stop_event()` 时把后面的钩子静默跳过；
- 状态在 `finally` 里先复位再合并：`_merge` 万一抛异常也不会把会话永久锁死；
- 每个会话一份独立状态（`Lock` + `Event`），超 64 个时回收空闲项。

## 5. 已知限制

- 群聊模式下，命中群的每条消息都会走一遍请求流程（见第 3 节）；
- 只在同一个会话内合并，跨会话不合并；
- 静默期间那条已被别的东西放行时，合并退化成原样放行，不报错；
- 图片 / 语音 / 文件按序补在文本之后，不保证和文本的相对位置完全一致。

## 6. 版本记录

- **1.1.2** 三层白名单由字符串改成列表（面板逐条增删），代码同时兼容列表与旧逗号字符串。
- **1.1.1** `scope_mode` 改下拉三选；`merge_separator`、`group_whitelist` 提示补全。
- **1.1.0** 通用版：新增 `scope_mode`、`group_whitelist`、`chat_whitelist`、`merge_separator`；会话判定改 `CustomFilter`，群聊不再被无条件唤醒。
- **1.0.1** 修 `target_users` 解析与 `debug` 默认值。
- **1.0.0** 首版：静默 6 秒 + 合并。

## 7. 许可

MIT，见 `LICENSE`。
