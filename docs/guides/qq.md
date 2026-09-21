# QQ 使用指南

[返回项目总览](../../README.md) · [使用指南目录](README.md) · [Web 使用指南](web.md)

QQ 入口将群聊消息交给统一的旅行 Agent，并在原群返回结果和提醒。可以选择 NapCat / OneBot 或 QQ 官方 Bot；不要在同一群同时开启两个回复入口，以免重复处理。

## 1. 选择接入方式

| 方式 | 前提 | 文档上传 | 启动入口 |
| --- | --- | --- | --- |
| NapCat / OneBot | NapCat 已登录，允许群消息和文件通知上报 | 允许群中直接发送支持的文件 | `adapters.onebot_app:create_runtime_app` |
| QQ 官方 Bot | QQ 开放平台应用、AppID / Secret 与对应消息能力 | 群内申请绑定码，私聊上传 | `run_bot.py` |

NapCat 的登录与协议可用性受 QQ 平台影响，本项目无法保证自动恢复风控或掉线。需要继续调试 Agent 时，可独立使用 Web；Web 不会自动导入 QQ 的聊天和行程。

## 2. Python 环境与公共配置

使用 Python 3.11，在项目根目录创建环境；已有 Conda 环境也可使用：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-onebot.txt -c constraints-py311.txt
Copy-Item .env.example .env
```

已有 `.env` 时不要覆盖。官方 Bot 可以只安装 `requirements.txt`；开发和完整回归测试使用 `requirements-dev.txt`。Linux/macOS 使用 `source .venv/bin/activate` 激活环境。

在项目根目录 `.env` 填写公共服务配置：

```dotenv
AMAP_API_KEY=replace_with_your_amap_web_service_key
LLM_API_KEY=replace_with_your_llm_api_key
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL_ID=replace_with_your_model_id
TRAVEL_SEMANTIC_MODE=execute
SEARCH_API_KEY=
```

- 高德 Key 在[高德开放平台](https://console.amap.com/dev/key/app)创建，平台类型选择 **Web 服务**。
- 模型需支持 Chat Completions 与原生工具调用；图片识别需要多模态能力。
- `SEARCH_API_KEY` 用于 Tavily 联网搜索，非必填；未配置时对应功能会提示不可用。
- `execute` 启用语义任务执行；`shadow` 仅记录语义识别结果，`preview` 不执行对应写操作。
- 修改配置后重启 Python 服务。真实 `.env`、Token、数据库与附件不可提交 Git。

## 3. NapCat / OneBot 本地运行

### Python 服务配置

```dotenv
ONEBOT_HTTP_URL=http://127.0.0.1:3000
ONEBOT_ACCESS_TOKEN=replace_with_outbound_token
ONEBOT_INBOUND_TOKEN=replace_with_different_inbound_token
ONEBOT_ALLOWED_GROUPS=123456789,987654321
ONEBOT_BIND_HOST=127.0.0.1
ONEBOT_BIND_PORT=8000
```

两个 Token 使用不同的随机值，生成示例：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

`ONEBOT_ACCESS_TOKEN` 用于 Python 调用 NapCat HTTP API；`ONEBOT_INBOUND_TOKEN` 用于验证 NapCat 发来的事件。允许群使用实际数字群号，多个群以逗号分隔；空白名单不会开放所有群。OneBot 不需要 QQ 官方 AppID / Secret。

启动后端：

```powershell
python -m uvicorn adapters.onebot_app:create_runtime_app --factory --host 127.0.0.1 --port 8000
```

此命令显式指定监听地址和端口，请与事件上报配置一致。代码升级后重启这个 Python 进程即可；NapCat 不必因此重新登录。

### NapCat 上报配置

在 NapCat 中配置：

1. HTTP API 监听 `3000`，Access Token 与 `ONEBOT_ACCESS_TOKEN` 一致。
2. HTTP 事件上报 URL 为 `http://127.0.0.1:8000/onebot`，客户端 Token 与 `ONEBOT_INBOUND_TOKEN` 一致。
3. 开启群消息和通知事件；群文件导入需要 `group_upload` 通知，以及 `/get_group_file_url` API。
4. 确保目标群已加入 `ONEBOT_ALLOWED_GROUPS`。容器内的 `127.0.0.1` 指向该容器，容器部署请使用后文的服务地址。

NapCat 使用客户端 Token 对请求体签名，服务支持 `X-Signature: sha1=...`；手工联调也可使用 `Authorization: Bearer <Token>` 或 `X-OneBot-Token`。

访问 `http://127.0.0.1:8000/health` 查看队列和后台任务状态。控制台显示收到群消息，不代表 HTTP 上报已经到达 Python；无回复时应分别检查两端日志。

### 触发与接续

- `@机器人`、固定命令、有效任务的本人短回答以及支持的旅行文档可以触发处理；普通闲聊不自动调用模型回复。
- 身份使用 `group_id + user_id`，昵称相同不会共享任务，改昵称不影响本人接续。
- 本人正在设置提醒时可以直接补充“上午十点”；其他成员的回答不会代为完成任务。
- “帮助”返回 OneBot 纯文本菜单；“上传文档”提示直接向当前群发送文件，不生成官方 Bot 的私聊绑定码。

未命中上述触发条件时，其他普通群聊和普通图片只保存为上下文，不自动调用模型回复；机器人的自身消息会被忽略。

## 4. QQ 官方 Bot

在 [QQ 开放平台](https://bot.q.qq.com/open)创建并配置机器人，填写：

```dotenv
QQ_BOT_APPID=replace_with_your_appid
QQ_BOT_SECRET=replace_with_your_appsecret
QQ_BOT_ALLOWED_GROUPS=replace_with_group_openid
QQ_BOT_ALLOW_ALL_GROUPS=false
```

官方 `group_openid` 与 OneBot 数字群号不同。空白名单默认拒绝群消息；不要将开发用的全群放行设置用于正式运行。

```powershell
python run_bot.py
```

日志出现 `Bot is online` 后，在允许群内 `@机器人 帮助` 或 `@机器人 状态` 测试。未获得全量群消息能力时，普通未 @ 消息不会被官方接口下发。

可在开放平台的指令配置中添加“帮助”“状态”“查询天气”“天气预报”“查询路线”“查询路况”“上传文档”。这些客户端入口需要在开放平台配置，代码不会自动注册。群聊按钮通常只填写命令，仍需用户发送。

## 5. 常用交互

官方 Bot 在以下消息前加 `@机器人`；OneBot 可按触发规则使用。

| 目的 | 示例 |
| --- | --- |
| 配置状态与帮助 | `状态`、`帮助` |
| 当前天气与预报 | `查询天气 武汉`、`天气预报 西宁` |
| 驾车路线与路况 | `查询路线 西宁 -> 青海湖`、`查询路况 张掖七彩丹霞 -> 敦煌` |
| 地点、步行与公交 | `推荐武汉的博物馆`、`武汉站到湖北省博物馆坐地铁怎么走` |
| 攻略研究 | `帮我搜索武汉三日游攻略` |
| 行程规划 | `帮我规划10月1日开始的武汉三天行程，必去湖北省博物馆` |
| 根据研究规划 | `根据研究结果规划行程，10月1日到10月三日` |
| 继续修改 | `希望轻松一些，尽量步行或公交地铁，少打车`、`第二天改为室内` |
| 确认与查看行程 | `确认行程修改`、`查看行程`、`我的行程` |
| 文字提醒 | `明天提醒我买车票`，再回答 `上午十点` |
| 提醒管理 | `查看我的提醒`、`买车票那条不用提醒了` |
| 预约开约提醒 | `10月1日去湖北省博物馆，开约时提醒我` |
| 定时查询 | `1分钟后告诉我武汉当前天气`、`查看定时查询` |
| 规则监测 | `监测湖北省博物馆预约规则，到10月1日`、`查看规则监测` |
| 长期偏好 | `记住，以后优先公共交通，轻松节奏`、`查看我的偏好` |
| 请求管理 | `查看任务进度`、`取消正在处理的请求`、`取消请求 编号` |

请用实际出行日期替换示例。行程支持 1–14 天；多个目标无法区分时先选目标。影响关联提醒的变更会先预览，确认后一起更新；已取消或手工指定时刻的提醒受到保护。

文字提醒、预约提醒、定时查询和规则监测是不同任务：“提醒我查天气”只发送文字，“到时告诉我天气”才在执行时调用查询。定时查询当前为一次性任务；规则监测按周期检查，未变化时不通知。

自然语言补充行程要求只影响当前行程，不自动保存为永久偏好。QQ 长期偏好限本人当前群；不会与 Web 或其他群共享。仅更新长期默认也不会重写旧行程。

## 6. 文档与图片

支持 `.txt`、`.md`、`.docx`、`.xlsx`，单文件最大 5 MB。旧 `.doc`、`.xls` 和 PDF 暂不作为旅行文档解析。Excel 导入可见工作表的文本、数字、日期及已缓存公式结果，不执行公式，也不导入图表和图片。

**OneBot**：直接向允许群发送文件。群文件消息与上传通知可能先后到达，适配器会通过 NapCat 获取下载地址并导入，不必重复上传。

**官方 Bot**：在目标群发送 `上传文档`，取得一次性 `QG-...` 绑定码；10 分钟内私聊机器人发送绑定码，收到绑定成功后，再单独发送文件。绑定码兑换一次后失效；如果日志 `attachments=0`，需检查官方 C2C 文件消息能力。

导入后可以问“文档里哪天去敦煌”或“根据文档规划行程”。群文档属于该群共享资料，临时对话和个人任务仍按成员隔离；不要把群资料当作成员私有存储。资料使用 SQLite FTS5 和原文片段检索，不要求向量数据库。

普通图片可 @ 机器人提问；明确要求“根据图片规划行程”才使用图片事实规划。图片里的文字不会被当作工具授权。预约攻略另有下述草稿确认流程。

### 景点预约图片与草稿

在允许的 QQ 群中先发送 `@机器人 制定预约`，机器人会进入 30 分钟的预约制定模式；随后发送一张景点预约攻略图片即可创建预约计划草稿。也可以在图片消息中直接填写 `制定预约`，一步启动并识别。普通图片不会再自动创建预约计划，为后续扩展通用识图功能保留独立入口。发送 `退出制定预约` 可以主动结束当前流程。

攻略图片支持 JPEG、PNG 和 WebP，单张最大 5 MB；一次发送多张图片会被拒绝，请逐张处理。附件必须提供有效的 HTTPS 下载地址，机器人收到后会立即下载，不能依赖之后继续访问 QQ 的临时链接。识图或下载失败时预约制定模式保持有效，可以修正图片后直接重试；成功生成草稿后模式自动结束。

处理流程如下：

```text
制定预约命令或活跃预约制定模式
→ 单张预约攻略图片
→ 保存原图并按 SHA-256 去重
→ 多模态模型提取景点、价格、开放时间和预约规则
→ 在群共享旅行文档中匹配明确游览日期
→ 生成待确认草稿
→ 创建者补充或修改
→ 明确确认后才创建正式提醒
```

模型只提取图片中明确存在的景点和预约规则。游览日期由 Python 从群共享资料中选择一份覆盖景点最多的最佳匹配行程文档，并仅在该文档内确定性匹配；同分时选择最新文档，不会跨行程版本拼接日期。预约日期按规则确定性计算：`提前 N 天` 按自然日回退，`提前 N 月` 按自然月回退，目标月份没有对应日期时取该月最后一天。识别不到景点或日期时不会猜测，而是保留与原图关联的全手动草稿。

除固定命令外，也可以直接使用自然语言，例如“按这张攻略帮我制定预约”“查看我的预约提醒并把 A-000123 改到 8 月 21 日”“刷新 R-20260722-001，然后确认”。图片创建草稿、查看、刷新、确认和修改都通过预约 Tool 执行。图片 Tool 只能读取当前消息中由程序注入的附件，平台、群和创建者身份同样由当前事件注入，模型不能提供外部图片 URL 或越权修改其他用户的数据。预约日期匹配、日期回退、确认和提醒持久化仍由确定性 Python 代码完成。

常用命令：

```text
制定预约
退出制定预约
补充预约 R-20260722-001 2 2026-08-20
新增预约 R-20260722-001 莫高窟 2026-08-20 提前1月
新增预约 R-20260722-001 黑独山 2026-08-22 无需预约
设置提醒 R-20260722-001 1 2026-08-15 07:30
设置提醒 R-20260722-001 1 2026-08-14 20:00, 2026-08-15 07:30
刷新预约 R-20260722-001
确认预约 R-20260722-001
取消预约 R-20260722-001
查看预约提醒
修改预约提醒 A-000123 游览日期 2026-08-21
修改预约提醒 A-000123 时间 2026-07-20 20:00, 2026-07-21 07:30
取消预约提醒 A-000123
```

`查看预约提醒` 会先使用群内最新行程文档刷新当前用户的未确认草稿；`确认预约` 在确认前执行相同刷新。也可以点击帮助或状态面板中的静态“刷新预约”按钮，按钮会填入 `刷新预约 `，补充计划编号后发送。刷新只处理 `needs_input` 和 `not_scheduled` 项目，不覆盖手工补充日期、已就绪项目或已确认计划。因此先发送预约图片、后上传 Excel 时，不需要重新发送图片。

需要预约且没有自定义时间时，默认在建议预约日前一天 `20:00` 和预约日当天 `09:00` 提醒。自定义的一个或多个完整时间会替换两条默认提醒。群内显示使用 `Asia/Shanghai`，数据库统一保存 UTC 时间。无需预约和无需提前统一按“无需预约”存档，不创建提醒。

计划必须由创建者明确确认；确认前不会发送提醒。只有创建者可以查看、确认、修改或取消自己的计划和提醒。此图片草稿流程不会代用户预约，也不会猜测官方链接；预约渠道为空时会提示核对。文字场馆预约规则查询是另一项能力，可读取已接入或用户明确提供的 HTTPS 来源。

到期消息通过已有 SQLite Outbox 发送。机器人离线期间错过、但游览日期尚未过去的提醒，会在下次启动时标记为延迟补发；游览日期已经过去的提醒会过期，不再发送；群被移出允许列表后会阻止发送。平台发送失败只重试已生成消息，不会再次识图、调用模型或重复创建提醒。

预约图片会发送到 `LLM_BASE_URL` 配置的外部多模态 API。原图保存在 `data/images/`，SQLite 保存文件路径、提取结果和提醒状态；`data/` 已排除在 Git 之外。日志不记录图片 URL、base64、完整 OCR 文本、密钥或群内私密内容，只记录哈希前缀、大小、模型、耗时和状态。

## 7. 持久化、提醒和备份

默认数据库为 `data/travel_bot.db`；`APP_DATA_DIR` 可改变 QQ 数据根目录，数据库名保持 `travel_bot.db`。Web 默认在独立的 `data/web/` 中保存，不应将两个入口指向同一数据库。

后端必须持续在线才能处理提醒。消息先持久化再执行，发送失败通过 Outbox 重试；重启会恢复待处理请求。过期和延迟提醒按各业务规则处理，不保证关机后补发所有消息。

“取消当前任务”结束待补充信息的对话；“取消正在处理的请求”停止后台请求。已经完成的操作不会回滚，正式提醒应单独取消。

聊天和终态事件默认保留 30 天，绑定码 7 天，预约及图片按相关 90 天策略清理；文档默认不自动过期，可用 `RETENTION_*_DAYS` 调整。备份应先停止服务，再复制整个数据目录，保留数据库与附件的一致性。

## 8. NapCat 容器部署

仓库保留 [NapCat 部署配置](../../deploy/napcat/)。需要持久运行的 Linux Docker 主机，不能把临时 GitHub-hosted Runner 当作 NapCat 登录状态的长期载体。此配置是 QQ 接入部署，不包括完整 Web 容器交付。

```bash
cd deploy/napcat
cp .env.example .env
# 填写两个不同 Token、允许群号、模型和高德配置
docker compose --env-file .env up -d --build
docker compose ps
```

`qq_config`、`napcat_config`、`travel_data` 持久卷分别保留登录、配置与业务数据。查看 Compose 文件中的实际镜像和端口设置。

NapCat WebUI 默认仅绑定服务器 `127.0.0.1:6099`，可以通过 SSH 隧道访问：

```bash
ssh -L 6099:127.0.0.1:6099 your-user@your-server
```

容器内 NapCat HTTP API 监听 `3000`；事件上报地址使用 `http://onebot:8000/onebot`，Token 与部署 `.env` 对应。启用群消息和 `group_upload` 通知。

切换官方 Bot 与 OneBot 前，先停止原入口或移除相应群白名单，再启动新入口；避免重复回复。升级或移除容器时保留持久卷。

仓库还保留官方 Bot 的 [GitHub Actions 定时启动工作流](../../.github/workflows/scheduled-bot.yml)。它需要相应仓库 Secrets 和加密状态备份配置，有运行时间和平台限制；不会提供持续在线的本地 Web，也不适合运行 NapCat 登录会话。

## 9. 检查与排错

离线回归：

```powershell
python -m pip install -r requirements-dev.txt -c constraints-py311.txt
python -X utf8 -m unittest discover -s tests -q
```

以下真实接口脚本使用临时数据和记录型投递，不向正式 QQ 群发消息，但会消耗已配置接口额度：

| 脚本 | 验证内容 |
| --- | --- |
| `python scripts/smoke_task_conversation.py` | 会话接续、提醒、调度 |
| `python scripts/smoke_booking_discovery.py` | 预约规则与地点查询 |
| `python scripts/smoke_trip_flow.py` | 行程及关联提醒 |
| `python scripts/smoke_inbox_media.py` | 消息队列、图片与重启恢复 |
| `python scripts/smoke_scheduled_query.py` | 延迟执行一次真实查询 |
| `python scripts/smoke_local.py --napcat` | 本地接口与 NapCat 连通性 |

| 现象 | 优先检查 |
| --- | --- |
| NapCat 在线但没有回复 | Python 8000 是否运行、事件 URL / Token、消息类型上报、群白名单 |
| QQ 风控下线 | NapCat 登录状态；可改用 Web 独立测试核心能力 |
| 固定命令有效，自然语言无效 | 模型三个配置项、工具调用支持、额度以及语义执行模式 |
| 高德 `INVALID_USER_KEY` | Key 是否正确且类型为 Web 服务 |
| 上传后没有资料 | 区分官方 Bot 私聊绑定与 OneBot 直接群文件流程；检查文件通知及附件数量 |
| 地点模糊或天气范围偏大 | 补充省市区；天气通常为行政区数据，不是景点微气候 |
| 路况缺少拥堵分段 | 高德可能未返回足够 TMC 数据，不代表道路畅通 |
| 进程重启后数据消失 | 检查 `APP_DATA_DIR`、容器持久卷及启动时使用的配置 |

路线时间和拥堵来自当前查询；未覆盖道路封闭、施工、结冰等信息。住宿和餐饮是地点候选，不代表已核实价格、库存或已完成预订。
