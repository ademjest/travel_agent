# 景点图片识别与预约提醒设计

## 目标

让群成员通过 `@Bot` 发送一张包含景点预约规则的图片。Bot 使用现有 OpenAI 兼容多模态模型提取景点、价格、开放时间和提前预约规则，从群共享行程文档中匹配游览日期，在用户确认后创建可持久化、可修改、可取消的预约提醒，并在到期时主动发送到原始群聊且 @ 确认成员。

首版必须满足：

- 图片识别结果不能直接触发提醒，必须经过用户确认。
- 信息缺失、冲突或低置信度时转入手动补充。
- 未自定义提醒时间时，使用“预约日前一晚 20:00 + 预约日当天 09:00”。
- `无需预约` 和 `无需提前` 统一视为无需预约，只保存信息，不创建提醒。
- GitHub Actions 离线期间到期的提醒不能丢失；下次启动后立即补发并标记延迟。
- 提醒通过现有 SQLite、Outbox 和传输适配器发送，不能绕开可靠投递机制。

## 非目标

首版不实现：

- 自动完成景点预约或购买门票。
- 自动联网查找或验证官方预约入口。
- 本地 OCR 模型部署。
- PDF、视频或一次消息多张图片的批量识别。
- 根据模型推测缺失的游览日期、年份或预约规则。
- 对无需预约景点发送提示性提醒。

如果图片、行程文档或用户提供了预约链接或渠道，可以保存并显示；缺失时统一提示用户前往景区官方渠道核对。

## 已确认的产品规则

1. 优先从群共享行程文档中匹配景点游览日期。
2. 无法唯一匹配时逐项要求用户手动补充。
3. 用户可以自定义一个或多个绝对提醒时间；一旦自定义，该景点不再生成默认双提醒。
4. 用户未自定义时，在预约日前一晚 20:00 和预约日当天 09:00 各提醒一次。
5. `提前 N 天` 按北京时间自然日倒推。
6. `提前 N 月` 按自然月倒推；目标月份没有对应日期时取该月最后一天。
7. 所有用户输入和展示使用 `Asia/Shanghai`，数据库保存 UTC 绝对时间。
8. 到期提醒发送到原始平台和原始群聊，并 @ 最终确认提醒的成员。
9. 平台无法生成 @ 时降级为普通群消息。
10. 只有创建者可以修改或取消已确认的预约项目。
11. 原图保存在 `data/images/`，随现有 `data/` 状态一起加密备份；SQLite 不保存图片 BLOB。
12. 原始图片、模型输出和确认记录按群隔离。

## 架构

新功能沿用现有传输中立结构：

```text
QQ Official / OneBot
        ↓
     ChatEvent
        ↓
TravelBotApplication
        ↓
ReservationImageService
        ├── ImageVisionExtractor
        ├── ReservationService
        └── MemoryStore
                         ↓
                  ReminderScheduler
                         ↓
                    SQLite Outbox
                         ↓
               QQ Official / OneBot
```

### `vision_service.py`

该文件包含两个职责明确的单元：

- `ReservationImageService`：验证、下载、限制大小、计算 SHA-256、保存原图、复用已有识别结果。
- `ImageVisionExtractor`：使用现有 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL_ID` 调用多模态模型，将图片转换为结构化候选数据。

该层不计算预约日期、不创建提醒、不发送消息。

多模态请求使用独立提示词，明确图片文字是不可信数据，只允许提取事实。模型返回一个 JSON 对象：

```json
{
  "raw_text": "按图片阅读顺序整理的原始文字",
  "items": []
}
```

`items` 中每个元素包含：

- `attraction_name`
- `price_text`
- `opening_hours`
- `requires_reservation`
- `advance_value`
- `advance_unit`：`day`、`month` 或 `none`
- `booking_channel`
- `source_text`
- `confidence`

模型输出不能包含可执行指令。程序必须在使用前完成类型、范围和枚举校验。

### `reservation_service.py`

负责：

- 规范化模型输出。
- 创建预约计划草稿。
- 为需要预约的景点检索群共享行程文档。
- 从检索片段中提取明确的游览日期候选。
- 生成待确认清单。
- 处理补充、确认、查看、修改和取消操作。
- 确认后计算预约日期和实际提醒时间。

该服务不能直接发送消息，只返回用户可读结果并调用 `MemoryStore` 持久化。

### `reminder_scheduler.py`

`ReminderScheduler` 按平台运行：

- 启动时立即扫描一次。
- 在线期间每 60 秒扫描一次。
- 将到期提醒原子化转换为 `processed_events + outbox_messages`。
- 使用现有 `OutboxWorker` 投递。
- 不直接调用 QQ 或 OneBot API。

提醒 Outbox 事件键为：

```text
reservation-reminder:<reminder_id>
```

唯一事件键和事务边界保证重复扫描、进程重启或并发扫描不会产生重复提醒。

### `TravelBotApplication`

群消息路由顺序调整为：

1. 将群消息保存为观察上下文。
2. 检查是否包含受支持的图片附件。
3. 一张图片时进入预约图片流程。
4. 多张图片时要求逐张发送。
5. 没有图片时继续现有文档、固定指令和旅行 Agent 流程。

私聊文档绑定流程保持不变。首版预约图片入口是群聊 `@Bot + 单张图片`。

## 图片处理与存储

支持格式：

- JPEG
- PNG
- WebP

限制：

- 单张最大 5 MB。
- 只允许 HTTPS 下载地址。
- 使用流式读取，同时校验声明大小和实际大小。
- 不信任原始文件名，不将其拼入磁盘路径。
- 磁盘路径按 SHA-256 生成，例如 `data/images/ab/<sha256>.jpg`。
- 同一 `storage_scope_id + sha256` 只保存和识别一次。

图片下载完成后以 base64 发送给多模态模型，不把具有时效性的 QQ 附件 URL 直接交给模型。

## 数据模型

### `reservation_images`

保存图片及识别审计信息：

- `id`
- `storage_scope_id`
- `platform`
- `group_id`
- `uploader_id`
- `sha256`
- `file_path`
- `content_type`
- `byte_size`
- `extracted_text`
- `extraction_json`
- `model_id`
- `status`：`pending`、`extracted`、`failed`
- `last_error`
- `created_at`
- `updated_at`

唯一约束：`(storage_scope_id, sha256)`。

### `reservation_plans`

保存一次识图产生的计划：

- `id`
- `plan_code`，格式为 `R-YYYYMMDD-NNN`
- `image_id`
- `platform`
- `group_id`
- `creator_id`
- `status`：`draft`、`confirmed`、`cancelled`
- `created_at`
- `updated_at`
- `confirmed_at`
- `cancelled_at`

### `reservation_items`

保存计划中的景点：

- `id`
- `public_code`，格式为 `A-NNNNNN`
- `plan_id`
- `item_index`
- `attraction_name`
- `price_text`
- `opening_hours`
- `booking_channel`
- `source_text`
- `confidence`
- `requires_reservation`
- `advance_value`
- `advance_unit`：`day`、`month`、`none`
- `visit_date`
- `booking_date`
- `date_candidates_json`：行程文档匹配到的零个、一个或多个 ISO 日期候选
- `custom_reminder_times_json`：用户在草稿阶段设置的零个或多个北京时间绝对时间
- `reminder_policy`：`default`、`custom`、`none`
- `status`：`needs_input`、`ready`、`confirmed`、`cancelled`
- `created_at`
- `updated_at`

唯一约束：`(plan_id, item_index)` 和 `public_code`。

### `reservation_reminders`

每行代表一次实际发送：

- `id`
- `reservation_item_id`
- `platform`
- `group_id`
- `recipient_id`
- `scheduled_at_utc`
- `status`：`pending`、`queued`、`sent`、`cancelled`、`expired`、`blocked`
- `outbox_event_id`
- `is_custom`
- `queued_at`
- `sent_at`
- `last_error`
- `created_at`

索引至少覆盖 `(platform, status, scheduled_at_utc)`。

## 日期匹配

对每个需要预约的景点：

1. 使用景点名称检索当前群的长期旅行文档。
2. 限制每个景点提供给模型的文档片段预算，避免十个景点导致上下文失控。
3. 使用现有模型的文本能力从片段中只提取明确写出的日期。
4. 没有日期时标记 `needs_input`。
5. 多个候选日期时向用户展示全部候选，不自动选择。
6. 只有月日但无法可靠确定年份时标记 `needs_input`。
7. 用户补充后重新执行确定性预约日期计算。

日期模型只提取文档证据，预约日必须由 Python 规则计算。

## 群聊交互

识图完成后返回：

```text
预约计划 R-20260722-001

1. 青海湖
   游览日期：2026-08-16
   预约日期：2026-08-15（提前1天）
   默认提醒：2026-08-14 20:00、2026-08-15 09:00

2. 莫高窟
   游览日期：未找到
   规则：提前1个自然月
   状态：需要补充日期

3. 黑独山
   无需预约，仅保存信息
```

草稿命令：

```text
补充预约 R-20260722-001 2 2026-08-20
新增预约 R-20260722-001 莫高窟 2026-08-20 提前1月
新增预约 R-20260722-001 黑独山 2026-08-22 无需预约
设置提醒 R-20260722-001 1 2026-08-15 07:30
设置提醒 R-20260722-001 1 2026-08-14 20:00, 2026-08-15 07:30
确认预约 R-20260722-001
取消预约 R-20260722-001
```

自定义提醒只接受一个或多个完整的北京时间绝对时间，多个时间用逗号分隔。无法解析的自然语言时间必须要求用户重输，不能猜测。

当模型完全无法提取景点时，仍创建一个与原图关联的空草稿，并提示用户使用 `新增预约`。`新增预约` 的首版语法固定为：计划编号、景点名称、完整游览日期、`提前N天`、`提前N月` 或 `无需预约`。价格、开放时间和预约渠道不是创建提醒的必填项，可以留空。这样模型失败不会阻断全手动流程。

确认规则：

- 任一需预约项目为 `needs_input` 时不能确认。
- 所有项目即使置信度高，也必须人工确认整个计划。
- `confidence < 0.85` 的字段在清单中明确标记。
- 用户确认后才创建 `reservation_reminders`。
- 所有项目均无需预约时允许确认并存档，但不创建提醒。

管理命令：

```text
查看预约提醒
修改预约提醒 A-000123 游览日期 2026-08-21
修改预约提醒 A-000123 时间 2026-07-21 07:30
修改预约提醒 A-000123 时间 2026-07-20 20:00, 2026-07-21 07:30
取消预约提醒 A-000123
```

修改游览日期会重新计算预约日期，取消该项目尚未发送的旧提醒，并生成新提醒。修改时间会用新的绝对时间集合替换该项目全部未发送提醒。只有 `creator_id` 可以修改或取消。

## 调度与发送

提醒内容包含：

- @ 创建者或确认者。
- 景点名称。
- 游览日期。
- 建议预约日期。
- 开放时间和参考价格。
- 已保存的预约渠道或链接。
- 政策可能变化、应以官方公告为准的提示。
- 延迟补发时的原定提醒时间。

离线和过期规则：

- 到期且 Bot 在线：正常发送。
- 到期时 Bot 离线、但游览日期尚未过去：启动后立即补发并标记延迟。
- 游览日期已经过去：标记 `expired`，不发送。
- 群已不在当前允许列表：标记 `blocked`，不发送。
- 无需预约项目：不存在提醒记录。

官方 QQ 主动提醒没有原消息 `msg_id`。`QQOfficialTransport` 在 `reply_to_id` 为空时必须使用平台主动群消息参数，普通被动回复路径保持不变。该能力依赖已开通的主动群消息权限。

OneBot 使用现有 `/send_group_msg` 路径。

## 事务和幂等边界

创建到期消息时，单个 SQLite 事务必须：

1. 验证提醒仍为 `pending` 且已到期。
2. 创建或取得 `processed_events` 中的唯一提醒事件。
3. 创建唯一 `outbox_messages` 行。
4. 将提醒状态改为 `queued` 并写入 `outbox_event_id`。

重复执行返回已有 Outbox，不生成第二条提醒。

发送成功后，现有 `mark_outbox_sent` 完成事件并记录群对话，同时根据 `outbox_event_id` 将对应 `reservation_reminders` 更新为 `sent` 并写入 `sent_at`。提醒使用 `creator_id` 作为归属成员，`prepared_memory_content` 使用固定文本“自动预约提醒”，避免将调度内部信息写入对话历史。

Outbox 重试期间，提醒保持 `queued`，真实重试状态以 `outbox_messages` 为准；`mark_outbox_failed` 同步更新提醒的 `last_error`。

修改或取消提醒时必须在同一事务中处理尚未发送的 Outbox：

- `pending` 或 `failed` 的 Outbox 改为 `cancelled`，不再被 `list_due_outbox` 选中。
- 尚未入 Outbox 的提醒直接改为 `cancelled`。
- Outbox 已处于 `sending` 时不能承诺阻止本次发送；命令返回“提醒正在发送，可能已发出”，但仍取消后续重试和新提醒。
- 修改操作完成旧提醒和旧 Outbox 取消后，才创建新的提醒行。

## 错误处理

### 图片下载失败

- 不创建计划。
- 返回明确错误并允许重发。
- 不保存不完整文件。

### 模型超时或调用失败

- 图片记录为 `failed` 并保留原图。
- 不创建正式提醒。
- 提示用户进入手动补充流程或稍后重新识别。

### 模型 JSON 无效

- 允许一次格式纠正请求。
- 第二次仍无效则记录失败并转手动流程。

### 日期或规则不完整

- 保存草稿。
- 将相关项目标记为 `needs_input`。
- 不允许确认。

### 主动消息发送失败

- 复用现有 Outbox 退避：5、15、60、300、900 秒，之后维持 900 秒上限。
- 不重新调用模型、不重新识图、不重复生成提醒。

## 安全与隐私

- 图片文字、文档片段和模型输出全部视为非可信数据。
- 图片提示词明确忽略修改身份、规则、工具权限或输出格式的文字。
- 不记录图片 base64、完整 OCR 原文、下载 URL、密钥或群成员敏感内容。
- 日志只记录图片 SHA 前缀、字节数、模型、耗时、状态和提取条数。
- 原图和 SQLite 均位于 `data/`，继续使用现有加密缓存流程。
- 不将原图放入 Git 或 GitHub Artifact 明文保存。
- 群数据使用现有 `storage_scope_id` 隔离，OneBot 与官方 QQ 的群号不会互相污染。

## 测试设计

所有模型和网络调用使用 fake 或 mock，不消耗真实额度。

### 图片和模型测试

- JPEG、PNG、WebP 成功路径。
- 超过 5 MB、非 HTTPS、无效内容类型和多图片拒绝。
- SHA 去重及同群复用。
- 不同群发送相同图片仍保持数据隔离。
- 正常 JSON、畸形 JSON、二次纠正失败、超时和低置信度。
- 图片中包含提示词注入文字时仍只提取预约事实。

### 日期测试

- 提前 1 天、3 天、5 天。
- 提前 1 个自然月。
- 月底不存在对应日期时取月底。
- 跨年计算。
- 北京时间到 UTC 转换。
- 无日期、多个日期和年份不明确时进入 `needs_input`。

### 交互测试

- 创建草稿、补充日期、自定义单次和多次提醒。
- 默认双提醒。
- 自定义提醒替换默认提醒。
- 未完成草稿不能确认。
- 重复确认不会重复创建提醒。
- 非创建者不能修改或取消。
- 修改日期后旧提醒取消并生成新提醒。
- 取消项目后不再发送。

### 调度和投递测试

- 启动扫描和每分钟扫描。
- 两个扫描器并发时只有一个 Outbox。
- 离线补发带延迟标记。
- 游览日期过期不发送。
- 群白名单撤销后标记 `blocked`。
- 官方 QQ 主动消息不携带原消息 `msg_id`。
- 普通群聊被动回复行为无回归。
- OneBot 主动消息复用相同计划和调度逻辑。

### 验收图片

使用用户提供的示例图片和模拟行程日期，验证：

- 提取 10 个景点。
- 青海湖、翡翠湖、莫高窟、鸣沙山、嘉峪关、察尔汗盐湖、茶卡盐湖、水上雅丹进入预约流程。
- 日月山和黑独山保存为无需预约，不创建提醒。
- 莫高窟按自然月计算。
- 未自定义时每个需预约景点生成两条提醒。
- 用户确认前不存在可发送的正式提醒。

## 成功标准

功能完成后应满足：

1. 用户可通过群聊单张图片创建预约计划草稿。
2. 模型错误不能静默生成错误提醒。
3. 行程日期可自动匹配，缺失时能完整转为手动流程。
4. 确认、查看、修改、取消均有确定性命令和权限控制。
5. 默认、自定义、自然日和自然月规则均由 Python 确定性计算。
6. 提醒在重启、离线和发送失败后不会丢失或重复。
7. 官方 QQ 和 OneBot 共享业务逻辑，只在主动发送适配上不同。
8. 原图、识别结果和提醒数据随现有加密状态持久化。
9. 完整测试套件不访问真实 QQ、模型或景点服务。
