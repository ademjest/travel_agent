from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re


TASK_TTL = timedelta(minutes=30)
READ_TASK_TYPES = {'weather', 'forecast', 'route', 'traffic', 'places', 'walking', 'transit'}


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    task_type: str
    status: str
    initial_request: str
    slots: dict
    missing_slots: tuple[str, ...]
    version: int
    expires_at: str
    last_event_id: str


@dataclass(frozen=True)
class TaskUpdate:
    task_type: str
    status: str
    initial_request: str
    slots: dict
    missing_slots: tuple[str, ...] = ()
    task_id: str = ""
    expected_version: int | None = None


def location_answer(content: str) -> str:
    text = re.sub(r"^(?:在|我在|查|查询|查一下)", "", content.strip())
    text = text.rstrip("。！？?! ")
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z· ]{2,30}", text):
        return ""
    if re.search(r"推荐|帮我|规划|安排|查询|吃饭|一起|明天|后天|提醒|取消|不用|不知道|谢谢|好的|聊天|文件|稍后|晚点|天气|怎么|什么", text):
        return ""
    return text


def weather_continuation(content: str, tasks: tuple[TaskRecord, ...]):
    candidates = [task for task in tasks
                  if task.task_type in {"weather", "forecast"}
                  and task.status == "collecting" and task.missing_slots == ("location",)]
    location = location_answer(content)
    if len(candidates) == 1 and location:
        task = candidates[0]
        return task, f"{task.initial_request}；查询地点：{location}"
    # A completed query may supply location for a short forecast follow-up.
    if re.fullmatch(r"(?:那|那么)?(?:明天|后天)(?:呢|的天气呢)?[？?。 ]*", content.strip()):
        candidates = [task for task in tasks if task.task_type in {"weather", "forecast"}
                      and task.status == "completed" and task.slots.get("location")]
        if candidates:
            task = candidates[0]
            return task, f"{task.slots['location']} {content} 天气预报"
    return None, content


def read_task_continuation(content, tasks):
    task, resolved = weather_continuation(content, tasks)
    if task:
        return task, resolved
    waiting = [task for task in tasks if task.task_type in READ_TASK_TYPES
               and task.status == 'collecting' and task.missing_slots]
    if len(waiting) != 1:
        return None, content
    task = waiting[0]
    location_fields = {'location', 'city', 'origin', 'destination'}
    compatible = bool(location_answer(content)) if set(task.missing_slots) <= location_fields else False
    if set(task.missing_slots) & {'origin', 'destination'} and len(content) < 120:
        compatible = compatible or bool(re.fullmatch(r'[^？?\n]+(?:到|至|->|→)[^？?\n]+', content))
    if compatible:
        labels = {'city': '城市', 'location': '地点', 'origin': '起点', 'destination': '终点'}
        fields = '、'.join(labels.get(name, name) for name in task.missing_slots)
        return task, f'{task.initial_request}\n用户补充{fields}：{content}'
    return None, content


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
