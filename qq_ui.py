import re
from typing import Any

from commands import parse_command


BUTTON_ROWS = (
    (
        ("help", "📖 帮助", "帮助", 0),
        ("status", "🧭 状态", "状态", 0),
        ("upload", "📎 上传资料", "上传文档", 0),
    ),
    (
        ("weather", "☀️ 查天气", "查询天气 ", 1),
        ("forecast", "🌦️ 查预报", "天气预报 ", 1),
    ),
    (
        ("route", "🚙 查路线", "查询路线 ", 1),
        ("traffic", "🚧 查路况", "查询路况 ", 1),
    ),
    (
        ("reservations", "⏰ 预约提醒", "查看预约提醒", 0),
        ("reservation-refresh", "🔄 刷新预约", "刷新预约 ", 1),
    ),
)


def _build_button(
    button_id: str,
    label: str,
    command: str,
    style: int,
) -> dict[str, Any]:
    return {
        "id": button_id,
        "render_data": {
            "label": label,
            "visited_label": label,
            "style": style,
        },
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": command,
            "reply": False,
            "enter": False,
            "unsupport_tips": f"请手动发送：{command.strip()}",
        },
    }


def build_command_keyboard() -> dict[str, Any]:
    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        _build_button(*button)
                        for button in row
                    ],
                }
                for row in BUTTON_ROWS
            ],
        },
    }


def build_help_markdown() -> str:
    return """# 🚙 青甘自驾助手

天气、路线和实时拥堵路段，都可以在群里快捷查询。

## 常用指令

- ☀️ `查询天气 西宁`
- 🌦️ `天气预报 青海湖`
- 🚙 `查询路线 西宁 -> 青海湖`
- 🚧 `查询路况 青海湖 -> 茶卡盐湖`
- 📎 `上传文档`

## 景点预约提醒

- 在群里 @机器人 并发送一张 JPEG、PNG 或 WebP 图片，单张最大 5 MB
- 识图结果必须先确认，确认前不会发送提醒
- 查看：`查看预约提醒`
- 刷新：`刷新预约 R-20260722-001`
- 补日期：`补充预约 R-20260722-001 2 2026-08-20`
- 自定义时间：`设置提醒 R-20260722-001 1 2026-08-15 07:30`
- 确认：`确认预约 R-20260722-001`

> 点击下方按钮可把指令填入输入框。天气、路线和路况指令还需要补充地点，然后确认发送。"""


def build_status_markdown(reply: str) -> str:
    status_items = [
        item.strip()
        for item in re.split(r"[；。]", reply)
        if item.strip()
    ]
    status_lines = "\n".join(f"- ✅ {item}" for item in status_items)
    return "\n".join([
        "# 🧭 旅行助手状态",
        "",
        status_lines or f"- ✅ {reply}",
        "",
        "> 需要查询时，可以直接点击下方快捷按钮。",
    ])


def build_group_message_payload(
    command_content: str,
    reply: str,
) -> dict[str, Any]:
    command = parse_command(command_content)
    if command.name == "help":
        markdown_content = build_help_markdown()
    elif command.name == "status":
        markdown_content = build_status_markdown(reply)
    else:
        return {"msg_type": 0, "content": reply}

    return {
        "msg_type": 2,
        "markdown": {"content": markdown_content},
        "keyboard": build_command_keyboard(),
    }
