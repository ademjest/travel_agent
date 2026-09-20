"""Build versioned task scenarios. Split is fixed before any live-model evaluation."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
cases = []


def add(category, **values):
    index = sum(case['category'] == category for case in cases) + 1
    cases.append(dict(id=f'{category}-{index:02}', category=category,
        split='holdout' if index % 4 == 0 else 'development', **values))


def step(text, *, at=False, **values):
    return dict(text=text, at=at, **values)


def read_script(city, forecast=False):
    name = 'get_weather_forecast' if forecast else 'get_current_weather'
    return [dict(tool=name, arguments={'location': city}), dict(text=f'{city}：晴。来源为评测天气快照。')]


chatter = ['明天一起吃饭', '这份文件我晚点发', '大家早上好', '收到啦', '谢谢大家',
           '今天好累', '路上慢点', '我先下线了', '晚安', '午饭吃什么呀']
for text in chatter:
    add('trigger', steps=[step(text, status='observed')], expect={'reminders': 0, 'calls': []})
for text in ['查看任务', '我的任务', '查看任务进度', '查看我的提醒', '我的提醒',
             '查看行程', '我的行程', '查看定时查询', '查看规则监测', '查看我的行程']:
    add('trigger', steps=[step(text, status='handled')], expect={'reminders': 0, 'calls': []})

for city in ['武汉', '西宁', '北京', '成都', '杭州']:
    for mode in ['plain', 'mention', 'forecast', 'chatter']:
        steps = [step('现在天气怎么样', at=True)]
        if mode == 'chatter':
            steps.append(step('明天一起吃饭', status='observed'))
        steps.append(step(city, at=mode == 'mention', responses=read_script(city)))
        names = ['get_current_weather']
        if mode == 'forecast':
            steps.append(step('那明天呢', responses=read_script(city, True)))
            names.append('get_weather_forecast')
        add('conversation', steps=steps, expect={'reminders': 0, 'calls': names, 'location': city})

for title in ['抢高铁票', '预约湖北省博物馆', '带身份证', '买10月1日车票', '收拾行李']:
    for mode in ['create', 'collect', 'semantic', 'edit', 'cancel']:
        text = f'明天上午十点提醒我{title}'
        steps = [step(text, at=True)]
        if mode == 'collect':
            steps = [step(f'明天提醒我{title}', at=True), step('上午十点')]
        if mode == 'semantic':
            steps = [step(f'提醒我明天上午十点{title}', at=True, responses=[dict(json={
                'action': 'create', 'title': title, 'time_text': '明天上午十点', 'question': ''})])]
        if mode in {'edit', 'cancel'}:
            steps.append(step('把刚才那条改成上午九点' if mode == 'edit' else '取消这条提醒'))
        add('reminder', steps=steps, expect={'reminders': 0 if mode == 'cancel' else 1,
            'calls': [], 'title': title, 'due': '2030-09-10T01:00:00+00:00' if mode == 'edit' else '2030-09-10T02:00:00+00:00'})

for days in [3, 5, 7, 10]:
    for mode in ['create', 'query', 'caveat', 'suspended', 'unknown']:
        policy = f'湖北省博物馆\n个人入馆预约可提前{days}天。每日0点开始放票。'
        if mode == 'caveat': policy += '节假日另行通知。'
        if mode == 'suspended': policy += '暂停预约。'
        if mode == 'unknown': policy = '湖北省博物馆\n请关注开放信息，预约范围另行通知。'
        text = '查询湖北省博物馆预约规则' if mode == 'query' else '我想10月1日去湖北省博物馆，开约时提醒我'
        steps = [step(text, at=True)]
        if mode == 'caveat': steps.append(step('按这个规则设置提醒'))
        add('policy', policy=policy, steps=steps, expect={'reminders': int(mode in {'create', 'caveat'}),
            'calls': [], 'policy_days': days, 'policy_mode': mode})

for length in [2, 3, 4, 5]:
    spec = dict(destination='武汉', duration_text=f'{length}天', start_date_text='10月1日',
        preferences=['每天2处景点'], must_visit=['湖北省博物馆'], budget_text='', transport_text='')
    plan = {'days': [dict(day_index=i+1, activities=[dict(poi_id=str(2*i+j), period=['上午', '下午'][j],
        reason='文化参观') for j in range(2)]) for i in range(length)]}
    for mode in ['create', 'date', 'cancel', 'indoor', 'reminder']:
        text = f'帮我规划10月1日开始的武汉{length}天行程，每天2处景点，必去湖北省博物馆'
        if mode == 'reminder': text += '，并提醒预约'
        steps = [step(text, at=True, responses=[dict(json=spec), dict(json=plan)])]
        if mode == 'date': steps.append(step('把行程改到10月2日开始'))
        if mode == 'cancel': steps.append(step('取消行程'))
        if mode == 'indoor': steps.append(step('第二天改为室内', responses=[dict(json=plan)]))
        add('trip', steps=steps, expect={'reminders': int(mode == 'reminder'), 'calls': [],
            'trips': int(mode != 'cancel'), 'days': length, 'start': '2030-10-02' if mode == 'date' else '2030-10-01',
            'indoor': mode == 'indoor'})

for city in ['武汉', '西宁', '北京', '成都', '杭州']:
    for mode in ['replay', 'restart', 'other_owner']:
        steps = [step('现在天气怎么样', at=True)]
        if mode == 'restart': steps.append(dict(control='restart'))
        if mode == 'other_owner': steps.append(step(city, user='other', status='observed'))
        steps.append(step(city, responses=read_script(city), message_id='answer'))
        if mode == 'replay': steps.append(step(city, message_id='answer'))
        add('recovery', steps=steps, expect={'reminders': 0, 'calls': ['get_current_weather'], 'location': city})

if __name__ == '__main__':
    target = ROOT / 'evals' / 'task_cases_v1.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({'version': 1, 'reference_time': '2030-09-09T02:00:00+00:00',
        'note': 'Synthetic future clock and tool snapshots. Offline scripts validate execution contracts, not model quality.',
        'cases': cases}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(cases)} cases -> {target}')
