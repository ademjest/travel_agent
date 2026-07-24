import re
from dataclasses import dataclass


HELP_TEXT = """🚙 青甘自驾助手

当前可用指令：
- 帮助 / 菜单：查看快捷操作
- 状态：查看机器人和数据源状态
- 查询天气 西宁：查询当前天气
- 天气预报 青海湖：查询未来天气
- 查询路线 西宁 -> 青海湖：规划驾车路线
- 查询路况 青海湖 -> 茶卡盐湖：查看实时拥堵路段
- 上传文档：获取私聊上传绑定码
- 制定预约：进入预约攻略图片识别流程
- ping：检查机器人是否在线

可以从群输入框的“/”指令面板选择，也可以在群里 @机器人 后输入。
路线指令推荐使用“->”分隔起点和终点。"""


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
        return Command(
            name="reservation_complete_date",
            args=match.groups(),
        )

    match = ADD_ITEM_RE.fullmatch(command)
    if match:
        plan_code, attraction, visit_date, rule, value, unit = match.groups()
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
        return Command(name="reservation_modify_date", args=match.groups())

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
