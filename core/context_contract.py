"""Complete structured projections shared by all model entry points."""
import json

from core.model_budget import ModelBudgetExceeded


POLICY_VERSION = 'context-v3'
READ_SLOT_FIELDS = {'location','city','origin','destination','keywords','reference_time','intents',
                    'trip_id','expected_trip_version','plan_artifact','budget_unit','period','title','trigger'}
POI_FIELDS = {'id','name','address','type','location','adname','cityname','rating','cost','lowest_price','queried_at'}


def task_projection(task):
    return {'type':task.task_type,'status':task.status,'missing':list(task.missing_slots),
            'request':task.initial_request,
            'slots':{key:value for key,value in task.slots.items() if key in READ_SLOT_FIELDS}}


def complete_json(value, max_chars):
    raw = json.dumps(value,ensure_ascii=False,separators=(',',':'))
    if len(raw) > max_chars:
        raise ModelBudgetExceeded('当前必要信息超过本次输入预算，请分段说明或缩小任务范围。')
    return raw


def project_places(places):
    return [{key:value for key,value in place.items() if key in POI_FIELDS} for place in places]


def response_text(response):
    choice = response.choices[0]
    if getattr(choice, 'finish_reason', None) in {'length', 'content_filter'}:
        raise ValueError('模型输出被截断或过滤，不能作为完整业务结果。')
    return choice.message.content or '{}'


def response_json(response):
    return json.loads(response_text(response))
