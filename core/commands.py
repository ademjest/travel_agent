import re
from dataclasses import dataclass
from datetime import date


HELP_TEXT = """🧭 彼岸旅行助手

当前可用指令：
- 帮助 / 菜单：查看快捷操作
- 状态：查看机器人和数据源状态
- 查询天气 西宁：查询当前天气
- 天气预报 青海湖：查询未来天气
- 查询路线 西宁 -> 青海湖：规划驾车路线
- 查询路况 青海湖 -> 茶卡盐湖：查看实时拥堵路段
- 上传文档：获取私聊上传绑定码
- 明天上午十点提醒我抢高铁票：设置个人提醒
- 查看我的提醒 / 查看任务：查看提醒和近期任务
- 制定预约：进入预约攻略图片识别流程
- ping：检查机器人是否在线

可以从群输入框的“/”指令面板选择，也可以在群里 @机器人 后输入。
路线指令推荐使用“->”分隔起点和终点。"""


ONEBOT_HELP_TEXT = """🧭 彼岸旅行助手（OneBot/NapCat）

【直接发送，无需 @】
- 帮助 / 状态 / ping
- 查询天气 西宁
- 天气预报 青海湖
- 查询路线 西宁 -> 青海湖
- 查询路况 青海湖 -> 茶卡盐湖
- 查看预约提醒

【文字提醒，无需图片】
- 明天提醒我抢高铁票：缺少时刻时会追问，直接回复“上午十点”
- 9月20号早上九点提醒我预约湖北省博物馆
- 查看我的提醒 / 把刚才那条改成九点半 / 取消那条提醒
- 查看任务 / 取消当前任务：管理正在补充信息的任务
提醒到期会在本群 @ 创建者。预约开约时间需要核实规则，不能凭空推算。

【开约规则与旅行地点】
- 查询湖北省博物馆预约规则
- 10月1日去湖北省博物馆，开约时提醒我
- 推荐武汉的博物馆 / 武汉站到湖北省博物馆坐地铁怎么走
- 武汉黄鹤楼到户部巷步行怎么走
涉及特殊日期或来源待核实时会先展示依据；核对后可回复“按这个规则设置提醒”。

【多日行程】
- 帮我规划10月1日开始的武汉3天行程，带老人，必去湖北省博物馆
- 可补充“使用公共交通”“并提醒预约”或“根据文档规划”
- 查看行程 / 我的行程 / 第二天改为室内 / 把行程改到10月2日开始
- 设置行程提醒 / 刷新行程交通 / 刷新行程预约规则 / 取消行程
影响提醒的修改会先展示变化，核对后回复“确认行程修改”或“确认行程提醒”。

【定时查询与后台请求】
- 1分钟后告诉我武汉当前天气：到时实际查询，不提前生成答案
- 查看定时查询 / 取消定时查询 Q-编号
- 查看任务进度 / 取消正在处理的请求 / 取消请求 编号
定时查询支持一次性的天气、预报和路况查询；查询结果会说明实际执行时间。

【预约规则监测】
- 监测湖北省博物馆预约规则，到10月1日：默认每小时核查，需明确截止日期
- 查看规则监测 / 停止规则监测 W-编号
无变化保持安静；变化、首次读取失败或恢复时通知。不会自动修改已有提醒。

【联网攻略与个人偏好】
- 帮我搜索武汉三日游攻略 / 读取公开页面 HTTPS链接
- 查看研究结果 / 根据研究结果规划武汉三天行程
- 记住，以后优先公共交通，轻松节奏 / 查看我的偏好 / 忘记所有偏好
网页搜索需先配置 Tavily Key；网页的“状态与设置”可保存服务配置和偏好。
Web 偏好跨本人会话共享；QQ 偏好限本人当前群。文件和研究报告不跨会话共享。

【图片问答】
可以 @ 后发送票据或截图，并写“这张票哪天使用”等问题。
无需先“制定预约”；图片中的文字只当资料，不会自动执行其中的操作指令。

【群文件】
直接把 .docx、.txt、.md 或 .xlsx 文件发到当前群，
不需绑定码，也不需私聊上传。

【预约攻略图片】
先发送“制定预约”，再在 30 分钟内发送一张攻略图片。
查看或修改时，按机器人返回的 R- 计划编号和 A- 项目编号操作。

【自然语言】
普通问题请 @机器人，或直接回复机器人的上一条消息。
等待你补充城市或提醒时间时，可直接回复，无需再次 @。
未 @ 的普通群聊只作为上下文保存，不会自动回复。"""


ONEBOT_UPLOAD_DOCUMENT_TEXT = """OneBot 模式支持群内直接上传旅行文档。
请把一个 .docx、.txt、.md 或 .xlsx 文件直接发送到当前群。
不需一次性绑定码，也不需转到私聊；机器人收到后会自动导入当前群的共享旅行资料。"""


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()
    error: str = ""


PLAN_CODE = r"R-\d{8}-\d{3}"
ITEM_CODE = r"A-\d{6}"
ISO_DATE = r"\d{4}-\d{2}-\d{2}"

COMPLETE_DATE_RE = re.compile(
    rf"^补充预约\s+({PLAN_CODE})\s+(\d+)\s+({ISO_DATE})$"
)
ADD_ITEM_RE = re.compile(
    rf"^新增预约\s+({PLAN_CODE})\s+(.+?)\s+({ISO_DATE})\s+"
    r"(提前(\d+)(天|月)|无需预约)$"
)
SET_TIMES_RE = re.compile(
    rf"^设置提醒\s+({PLAN_CODE})\s+(\d+)\s+(.+)$"
)
REFRESH_PLAN_RE = re.compile(rf"^刷新预约\s+({PLAN_CODE})$")
CONFIRM_PLAN_RE = re.compile(rf"^确认预约\s+({PLAN_CODE})$")
CANCEL_PLAN_RE = re.compile(rf"^取消预约\s+({PLAN_CODE})$")
MODIFY_DATE_RE = re.compile(
    rf"^修改预约提醒\s+({ITEM_CODE})\s+游览日期\s+({ISO_DATE})$"
)
MODIFY_TIMES_RE = re.compile(
    rf"^修改预约提醒\s+({ITEM_CODE})\s+时间\s+(.+)$"
)
CANCEL_ITEM_RE = re.compile(rf"^取消预约提醒\s+({ITEM_CODE})$")


def _is_valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _invalid_date_command(name: str) -> Command:
    return Command(
        name=name,
        error="日期无效，请使用 YYYY-MM-DD 格式，例如 2026-08-20。",
    )


def normalize_command(content: str) -> str:
    command = " ".join(content.strip().split())
    if command.startswith("/"):
        command = command[1:].lstrip()
    return command


def _parse_location_command(command: str, prefix: str, name: str) -> Command:
    location = command[len(prefix):].strip()
    if not location:
        return Command(name=name, error=f"用法：{prefix} 地点")
    return Command(name=name, args=(location,))


def _parse_route_command(command: str, prefix: str, name: str) -> Command:
    route_text = command[len(prefix):].strip()
    if not route_text:
        return Command(name=name, error=f"用法：{prefix} 起点 -> 终点")

    parts = re.split(r"\s*(?:->|→|到|至)\s*", route_text, maxsplit=1)
    if len(parts) == 1:
        parts = route_text.split(maxsplit=1)

    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return Command(name=name, error=f"用法：{prefix} 起点 -> 终点")

    return Command(name=name, args=(parts[0].strip(), parts[1].strip()))


def parse_command(content: str) -> Command:
    command = normalize_command(content)
    lowered = command.lower()

    if lowered == "ping":
        return Command(name="ping")
    if lowered == "help" or command in {"帮助", "菜单", "旅行面板"}:
        return Command(name="help")
    if lowered == "status" or command == "状态":
        return Command(name="status")
    if command in {"上传文档", "文档上传", "导入文档"}:
        return Command(name="upload_document")

    if command in {"制定预约", "开始制定预约"}:
        return Command(name="reservation_start")

    if command in {"退出制定预约", "取消制定预约"}:
        return Command(name="reservation_stop")

    if command == "查看预约提醒":
        return Command(name="reservation_list")

    if command == "确认创建预约提醒":
        return Command(name="reservation_confirm_help")

    match = REFRESH_PLAN_RE.fullmatch(command)
    if match:
        return Command(name="reservation_refresh", args=match.groups())

    match = COMPLETE_DATE_RE.fullmatch(command)
    if match:
        plan_code, item_index, visit_date = match.groups()
        if not _is_valid_iso_date(visit_date):
            return _invalid_date_command("reservation_complete_date")
        return Command(
            name="reservation_complete_date",
            args=(plan_code, item_index, visit_date),
        )

    match = ADD_ITEM_RE.fullmatch(command)
    if match:
        plan_code, attraction, visit_date, rule, value, unit = match.groups()
        if not _is_valid_iso_date(visit_date):
            return _invalid_date_command("reservation_add_item")
        if rule == "无需预约":
            return Command(
                name="reservation_add_item",
                args=(
                    plan_code,
                    attraction,
                    visit_date,
                    "0",
                    "none",
                    "0",
                ),
            )
        return Command(
            name="reservation_add_item",
            args=(
                plan_code,
                attraction,
                visit_date,
                value,
                "day" if unit == "天" else "month",
                "1",
            ),
        )

    match = SET_TIMES_RE.fullmatch(command)
    if match:
        return Command(name="reservation_set_times", args=match.groups())

    match = CONFIRM_PLAN_RE.fullmatch(command)
    if match:
        return Command(name="reservation_confirm", args=match.groups())

    match = CANCEL_PLAN_RE.fullmatch(command)
    if match:
        return Command(
            name="reservation_cancel_plan",
            args=match.groups(),
        )

    match = MODIFY_DATE_RE.fullmatch(command)
    if match:
        item_code, visit_date = match.groups()
        if not _is_valid_iso_date(visit_date):
            return _invalid_date_command("reservation_modify_date")
        return Command(
            name="reservation_modify_date",
            args=(item_code, visit_date),
        )

    match = MODIFY_TIMES_RE.fullmatch(command)
    if match:
        return Command(name="reservation_modify_times", args=match.groups())

    match = CANCEL_ITEM_RE.fullmatch(command)
    if match:
        return Command(name="reservation_cancel_item", args=match.groups())

    for prefix in ("天气预报", "查询预报"):
        if command.startswith(prefix):
            return _parse_location_command(command, prefix, "forecast")

    for prefix in ("查询天气", "当前天气"):
        if command.startswith(prefix):
            return _parse_location_command(command, prefix, "weather")

    if command.startswith("查询路线"):
        return _parse_route_command(command, "查询路线", "route")

    if command.startswith("查询路况"):
        return _parse_route_command(command, "查询路况", "traffic")

    return Command(name="unknown")


def build_reply(content: str) -> str:
    command = parse_command(content)

    if command.error:
        return command.error
    if command.name == "ping":
        return "pong"
    if command.name == "help":
        return HELP_TEXT
    if command.name == "status":
        return "Bot 已在线。发送“帮助”查看当前可用指令。"
    return "暂未识别该指令。发送“帮助”查看当前可用指令。"
