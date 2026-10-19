from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import re


TASK_TTL = timedelta(minutes=30)
READ_TASK_TYPES = {'weather', 'forecast', 'route', 'traffic', 'places', 'walking', 'transit'}
ROUTE_TASK_TYPES = {'route','traffic','walking','transit'}


def route_correction(content):
    if re.search(r'行程|提醒|预约|取消',content):
        return {}
    pattern=r'(起点|出发地|终点|目的地)(?:不是[^，,；;。]+[，,]\s*(?:而)?是|(?:应该|应当|其实)?是|改为|改成|换成|更正为|应为)\s*([^，,；;。！？?!\n]+)'
    changes={}
    for match in re.finditer(pattern,content):
        value=match[2].strip()
        address=re.match(r'[，,]\s*(?:具体(?:地址)?(?:是|为|在)?|地址(?:是|为|在)?|位于)\s*[:：]?\s*([^，,；;。！？?!\n]+)',content[match.end():])
        if address and re.search(r'[路街巷].*?\d+号',address[1]):
            value=content[match.start(2):match.end()+address.end()].strip()
        if 1<=len(value)<=150:
            changes['origin' if match[1] in {'起点','出发地'} else 'destination']=value
    return changes


def route_correction_candidates(tasks):
    return [task for task in tasks if task.status in {'collecting','completed','failed'}
            and (task.task_type in ROUTE_TASK_TYPES or set(task.slots.get('intents',[]))&ROUTE_TASK_TYPES)
            and task.slots.get('origin') and task.slots.get('destination')]


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
    if (not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9· ]{2,30}", text)
            or not re.search(r'[\u4e00-\u9fffA-Za-z]',text)):
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
        if len(candidates) == 1 or (candidates and len({task.slots['location'] for task in candidates}) == 1):
            task = candidates[0]
            return task, f"{task.slots['location']} {content} 天气预报"
    return None, content


def read_query_message(task):
    if task.task_type not in ROUTE_TASK_TYPES and not set(task.slots.get('intents',[]))&ROUTE_TASK_TYPES:
        return task.initial_request
    labels={'origin':'起点','destination':'终点','city':'城市'}
    values='；'.join(label+'：'+task.slots[field] for field,label in labels.items() if task.slots.get(field))
    return f'继续{task.task_type}查询，以当前地点为准：{values}'


def read_task_continuation(content, tasks):
    changes=route_correction(content)
    if changes:
        candidates=route_correction_candidates(tasks)
        pairs={(re.sub(r'[\s的]','',task.slots['origin']),re.sub(r'[\s的]','',task.slots['destination'])) for task in candidates}
        if candidates and len(pairs)==1:
            task=candidates[0]
            slots={**task.slots,**changes}
            amended=replace(task,slots=slots)
            values='；'.join(('起点' if field=='origin' else '终点')+'：'+value for field,value in changes.items())
            return amended,read_query_message(amended)+'\n用户更正'+values
        return None,content
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
        return task, f'{read_query_message(task)}\n用户补充{fields}：{content}'
    return None, content


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
