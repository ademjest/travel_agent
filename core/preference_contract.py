"""Shared preference fields and validation for storage, intent and settings UI."""
import re

BUDGET_CHOICES = {'currency':('CNY','USD','EUR'),'basis':('每人','总额'),
                  'period':('每天','全程'),'category':('住宿','餐饮','交通','旅行总预算')}

LABELS = {'pace': '行程节奏', 'transport': '交通偏好', 'interests': '兴趣类别', 'night': '夜间活动',
          'departure_city': '常用出发城市', 'budget': '默认预算', 'reply_style': '回复风格'}
CHOICES = {'pace': ('轻松', '适中', '紧凑'), 'transport': ('公共交通', '步行', '驾车'),
           'night': ('不安排夜游', '可以夜游'), 'reply_style': ('简洁', '详细', '表格')}
INTERESTS = ('博物馆', '历史街区', '自然景观', '美食', '公园', '艺术')


def validate_preferences(values):
    if not isinstance(values, dict) or set(values) - LABELS.keys():
        raise ValueError('不支持的偏好字段。')
    for key, value in values.items():
        if value is None:
            continue
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError('偏好值不在支持范围内。')
        if key == 'interests' and (not isinstance(value, list) or not value or len(value) > 6 or any(v not in INTERESTS for v in value)):
            raise ValueError('请选择支持的兴趣类别。')
        if key == 'departure_city':
            if not isinstance(value, str) or not re.fullmatch(r'[\u4e00-\u9fffA-Za-z· ]{2,30}', value):
                raise ValueError('请提供城市名称，不保存详细住址。')
        if key == 'budget':
            if (not isinstance(value, dict) or set(value) != {'amount', 'currency', 'basis', 'period', 'category'}
                    or type(value['amount']) not in (int, float) or not 0 < value['amount'] <= 1000000
                    or value['currency'] not in BUDGET_CHOICES['currency'] or value['basis'] not in BUDGET_CHOICES['basis']
                    or value['period'] not in BUDGET_CHOICES['period'] or value['category'] not in BUDGET_CHOICES['category']):
                raise ValueError('预算需要金额、币种、每人/总额、每天/全程及适用项目。')



def preference_fields():
    return {'labels':LABELS, 'choices':CHOICES, 'interests':INTERESTS,
            'budget':BUDGET_CHOICES}


def preference_change(value, source_text):
    """Validate the versioned semantic proposal; ownership stays server-bound."""
    fields = {'version','subject','scope','polarity','values','evidence','authorization'}
    if not isinstance(value,dict) or value.keys()-fields or type(value.get('version',1)) is not int or value.get('version',1) != 1:
        raise ValueError('偏好解析合同无效。')
    for key, options in {'subject':{'self','other','unclear'},'scope':{'long_term','current_request','unclear'},
                         'polarity':{'affirmed','negated','unclear'}}.items():
        if not isinstance(value.get(key),str) or value[key] not in options:
            raise ValueError('偏好主体、作用范围或肯否尚不明确。')
    values, evidence = value.get('values'), value.get('evidence')
    validate_preferences(values)
    if not isinstance(evidence,dict) or evidence.keys() != values.keys():
        raise ValueError('每项偏好需要对应的原话依据。')
    if any(not isinstance(quote,str) or not quote or len(quote)>500 or quote not in source_text
           for quote in evidence.values()):
        raise ValueError('偏好依据无法对应本轮原话。')
    for key, item in values.items():
        if item is None:
            continue
        quote = evidence[key].replace('不夜游','不安排夜游').replace('不安排晚上','不安排夜游')
        quote = quote.replace('人民币','CNY').replace('美元','USD').replace('欧元','EUR')
        atoms = item.values() if isinstance(item,dict) else item if isinstance(item,list) else [item]
        for atom in atoms:
            literal = format(atom,'g') if type(atom) in {int,float} else str(atom)
            matches = list(re.finditer(re.escape(literal),quote))
            if not any(not re.search(r'(?:不|不喜欢|不偏爱|不要|不优先|不是|别用)$',quote[:m.start()]) for m in matches):
                raise ValueError('偏好字段值缺少肯定的原文依据。')
    authorization = value.get('authorization','')
    if not isinstance(authorization,str) or len(authorization)>200 or (authorization and authorization not in source_text):
        raise ValueError('偏好保存请求无法对应本轮原话。')
    return {**value,'version':1,'authorization':authorization}


def explicit_preference_write(text, *, forget=False):
    verb = r'忘记|删除|清除' if forget else r'记住|保存|更新'
    return bool(re.match(r'^(?:(?:请|帮我)|我(?:希望|想让)你)?(?:'+verb+r')',text.strip())
                or (not forget and re.match(r'^以后(?:都)?(?:默认|优先)',text.strip())))
