# 语义编译与复合任务升级总结

日期：2026-09-13  
项目：`E:\Agent\travel_agent`  
运行环境：`D:\anaconda3\envs\agent\python.exe`（Conda `agent`）

## 交付结论

本批按已审核的三个阶段完成了自然语言入口升级：

1. LLM 将自然语言编译为版本化 `Intent IR v1`，影子模式不产生业务写入。
2. 语义结果可读取权威行程，并把按日期表达转换为活动级修改预览。
3. 用户确认后，行程活动删除和关联预约提醒取消沿用现有版本校验，在同一事务中提交。

普通文字提醒仍然独立存在；预约图片、旧 `R-`/`A-` 预约草稿和天气/交通能力保持兼容。

本次执行沿用本地已有修改，不重置工作区、不提交或推送 Git，也不替换正式数据库。

## 阶段一：Intent IR 与影子解析

新增：

- `core/intent_ir.py`：定义版本、动作、领域、操作、原文依据和确认标记。
- `agents/intent_compiler.py`：调用模型生成 IR，不授权写入。
- `tests/test_intent_ir.py`：验证复合操作、自然确认、权属字段禁止生成、闲聊识别和 JSON 校验。

模型输出中的 `trip_id`、`reminder_id`、`plan_code`、`owner_id`、`group_id` 等字段会被拒绝，嵌套对象中的同类字段也会被拒绝。`source_spans` 必须是用户原话中的连续片段，不能用模型补写的实体作为依据。

稳定性检查：

```powershell
conda activate agent
cd E:\Agent\travel_agent
$env:TRAVEL_SEMANTIC_MODE='shadow'
python -X utf8 -m unittest discover -s tests -p test_intent_ir.py -q
```

在 QQ 测试群中发送几条自然语言修改请求后，检查 Bot 日志中是否出现 `Semantic shadow IR`。影子模式只能记录解析结果，不能新增提醒、修改行程或取消提醒；数据库中 `trips`、`personal_reminders` 和 `reminder_occurrences` 应保持不变。

真实模型冒烟脚本 `scripts/smoke_semantic_compiler.py` 已解析“按日期删除活动”“自然确认”和“普通五分钟提醒”三类消息。复合请求的 IR 结构由本地伪模型和真实 SQLite 事务专项测试覆盖；追加复合原文到外部模型端点的尝试被自动审查拦截，因此报告不把它计作真实模型联网通过。

根据 QQ 阶段测试反馈，编译器现在记录具体校验失败原因；当模型无法抽取地点时，语义服务会持久化 `semantic` 待补任务，且入口策略允许同一用户用未 @ 的短消息继续，后续只发送地点名称也会接续到原请求，不再退化为普通地点查询。

## 阶段二：只读和预览

新增：

- `services/semantic_task_service.py`：把通过校验的 IR 交给已有领域服务。
- 行程服务支持 `force_confirmation`，自然语言删除活动始终先生成预览。
- “你还记得我的行程吗”等表达可转为权威的当前行程读取。
- 日期表达会根据活动日期定位行程中的第几天，再复用已有行程修改校验。

例如“10月2日不去湖北省博物馆了”会转换为内部的“第1天不去湖北省博物馆”，但在预览阶段不会修改数据库。

稳定性检查：

```powershell
$env:TRAVEL_SEMANTIC_MODE='preview'
python -X utf8 -m unittest discover -s tests -p test_semantic_task_service.py -q
```

人工检查时，先发送自然语言查看行程，再发送“10月2日不去湖北省博物馆了”。应看到修改预览；随后用“查看行程”核对，行程版本和提醒时间仍未改变。若用户有多份行程，系统应要求选择具体行程。

## 阶段三：确认后的事务执行

复用已有：

- `TripService._reminder_changes()`：按行程关联关系计算提醒变化。
- `TripRepository.commit()`：行程、版本快照和提醒变化同事务提交。
- `TaskRepository`：保存待确认预览及版本信息。
- `ReminderRepository`：校验提醒所有者和提醒版本。

新增语义确认支持：

```text
确认取消调整，请变更行程
```

会被编译为 `trip/confirm_pending_operation`，再转换为既有的“确认行程修改”流程。确认前行程和提醒不变；确认成功后，目标活动被删除，关联预约提醒被取消，其他日期和普通提醒保持不变。

端到端专项测试验证了：

- 自然语言按日期删除活动；
- 生成预览而不提前写入；
- 自然语言确认；
- 行程活动与关联预约提醒同时变更；
- 行程版本递增；
- 其他用户不能读取或修改；
- 普通提醒不会被当作关联提醒。

稳定性检查：

```powershell
$env:TRAVEL_SEMANTIC_MODE='execute'
python -X utf8 -m unittest discover -s tests -p test_semantic_task_service.py -q
python -X utf8 -m unittest discover -s tests -p test_trips.py -q
```

QQ 人工验收建议使用隔离测试群：

1. 查看一份已有武汉行程，确认其中有 2026-10-02 的湖北省博物馆和关联预约提醒。
2. 发送“10月2日不去湖北省博物馆了”，确认机器人只展示预览。
3. 发送“确认取消调整，请变更行程”。
4. 再发送“查看行程”和“查看我的提醒”。
5. 确认湖北省博物馆已从该行程移除，关联提醒已取消，其他提醒（例如登录王者做任务）没有改变。

## 路由和安全边界

应用层在 [app/bot_application.py](../app/bot_application.py) 的群消息处理中增加语义任务服务。语义服务先于行程、预约和普通提醒服务运行，但只对包含动作或行程词的消息调用编译器；固定控制命令仍走快速路径。

LLM 只提出意图，程序负责：

- 从 OneBot 事件获得平台、群号、QQ 号和事件时间；
- 根据当前用户权限解析行程和提醒；
- 解析最终日期和时间；
- 校验资源状态和版本；
- 决定是否需要确认；
- 执行事务和 Outbox 投递。

编译器不可用或输出校验失败时，语义服务放弃本轮处理，旧的确定性服务继续接管；因此固定指令和既有功能不会因为模型临时不可用而失效。

## 自动验证

最终整套测试：**520 项通过，196.299 秒**。新增语义编译专项 4 项、语义服务专项 10 项和运行时 rollout 配置专项 1 项，其中包含真实 SQLite 行程与关联提醒事务流程。`python -m py_compile` 和 `git diff --check` 通过。

上一轮已有的 120 个离线任务场景、29 个真实模型保留场景、天气/提醒/行程/图片/Inbox/规则监测测试仍保持通过；本批没有改变原有测试的固定模型或真实 QQ 发送约束。

## 配置和回滚

通过环境变量控制阶段：

```text
TRAVEL_SEMANTIC_MODE=shadow   # 只记录 IR
TRAVEL_SEMANTIC_MODE=preview  # 允许读取和生成预览，不执行语义写操作
TRAVEL_SEMANTIC_MODE=execute  # 允许确认后的确定性事务执行
```

当前代码默认使用 `execute`，但固定命令仍不依赖语义编译器。建议首次 QQ 验收先设置为 `shadow`，确认日志中的 IR 稳定后再切换 `preview`，最后切换 `execute`。若真实 QQ 验收发现模型解析不稳定，可临时切回 `shadow`，重启 Python 服务后继续使用旧确定性流程，不需要修改数据库。

## 已知限制

- 语义编译器仍受模型质量影响；输出会被严格校验，无法唯一匹配时会要求补充信息。
- 目前复合执行重点覆盖行程活动和行程派生预约提醒，尚未把任意多个旅行领域动作都纳入同一事务。
- 旧预约计划仍使用原有编号和工具体系，不能把它们与行程关联提醒混为同一种资源。
- 本批自动测试使用临时数据库和记录型 Transport；真实 QQ 未 @ 接收、引用展示和到期投递仍需人工验收。

本批未提交 Git、未推送远端、未替换正式数据库，也未向 QQ 群发送自动测试消息。

## 2026-09-14 QQ 现场反馈修正

用户在 shadow、preview、execute 三种模式下都收到了相同的地点查询回复。只读访问当前 `http://127.0.0.1:8000/health` 时，返回结果没有新代码必定包含的 `semantic_compiler` 字段，说明当时处理 QQ 消息的 Python 进程没有加载本轮最新代码。

本次增加：

- `/health` 返回 `semantic_compiler.enabled` 和 `semantic_compiler.mode`；
- 所有通过校验的 IR 记录 `mode`、来源（model/recovery）、动作和操作类型；
- 模型把明确的行程删除误分为地点查询时，根据当前用户真实行程中的唯一地点恢复为 `trip/remove_activity`；
- 模型漏填地点、但用户原文已经包含行程中的唯一地点时，程序直接恢复该地点；
- 真正缺少地点时持久化 semantic 待补任务，未 @ 的短回答也能接续。

修正后全套 **520 项通过，168.305 秒**。重启后先执行：

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/health).semantic_compiler
```

应返回类似：

```text
enabled mode
------- ----
True    preview
```

若字段不存在，仍然是旧进程或从其他项目目录启动；若 `enabled=False`，模型配置没有被运行时识别；若 mode 与终端设置不一致，应检查启动命令所在终端的环境变量并重新启动。

第三阶段现场测试还发现同一行程存在两条重复待确认预览。此前 `trip_continuation` 要求全局只有一条 collecting 行程任务，因此确认返回“没有仍然有效的预览”。现在创建新预览时会自动把同一行程的旧预览标为 `superseded`；对于升级前已存在的重复预览，确认会在同一行程范围内选择最新一条，多个不同旅行的预览仍不会被静默合并。
