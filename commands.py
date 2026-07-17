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
- ping：检查机器人是否在线

可以从群输入框的“/”指令面板选择，也可以在群里 @机器人 后输入。
路线指令推荐使用“->”分隔起点和终点。"""


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()
    error: str = ""


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
