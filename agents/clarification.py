import re
from datetime import datetime, timedelta, timezone


def missing_weather_location(text: str) -> bool:
    return bool(re.fullmatch(
        r"(?:请问|请|帮我|帮忙|查询|查一下|看看|我想知道|现在|当前|实时|今天|明天|后天|"
        r"未来几天|天气预报|天气|气温|怎么样|如何|怎样|多少|呢|吗|的|[？?。\s])+", text))


def is_weather_clarification(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:请(?:问|告诉我|提供)?|告诉我|或|当前|重新|你|您|想|要|需要|查询|查|的|是|天气|预报|"
            r"一下|我|帮你|帮您|哪个|哪里|城市|地区|地点|位置|具体|呢|吗|[，,？?。\s])+",
            text,
        )
        and any(word in text for word in ("哪个", "哪里", "请提供", "告诉我"))
    )


def resume_weather_request(content: str, recent_dialogue) -> str:
    # Only a short location answer to the latest, unexpired clarification is resumed.
    if not recent_dialogue or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z· ]{1,30}", content):
        return content
    turn = recent_dialogue[-1]
    if not is_weather_clarification(turn.assistant_content):
        return content
    try:
        created = datetime.fromisoformat(turn.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(minutes=30):
            return content
    except ValueError:
        return content
    from agents.travel_decision import decide_travel_action
    if (set(decide_travel_action(turn.user_content).intents) <= {"weather", "forecast"}
            and decide_travel_action(content).intent == "general"):
        return f"{turn.user_content}；查询地点：{content}"
    return content
