"""A bounded provider phase, with a separately inspectable complete answer."""
from datetime import datetime, timedelta, timezone
import re

from core.wendao_answer import extract_candidates, answer_targets, VERSION
from core.rule_semantics import resolve_rules
from core.planning_dates import apply_reference_date, explicitly_undated
from services.reminder_time import number, parse_reminder_time
from core.trip_updates import date_range
from infrastructure.wendao_client import WendaoError


def reference_dates(event):
    spec = {'day_count': 1}
    count = re.search(r'([一二两三四五六七八九十\d]+)(?:天|日游)', event.content)
    if count:
        spec['day_count'] = min(14, number(count[1]))
    raw = re.search(r'20\d{2}年\d{1,2}月\d{1,2}日|20\d{2}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日|明天|后天', event.content)
    now = event.occurred_at or datetime.now(timezone.utc)
    if not explicitly_undated(event.content):
        try:
            interval = date_range(event.content, now)
        except ValueError:
            raise WendaoError('invalid_reference_date') from None
        if interval:
            spec.update(start_date=interval['start'], start_date_text=interval['start_text'],
                        end_date=interval['end'], day_count=interval['days'], date_is_tentative=False)
            return spec
    if raw and not explicitly_undated(event.content):
        parsed = parse_reminder_time(raw[0], now)
        if parsed.day and not parsed.error:
            spec.update(start_date=parsed.day, start_date_text=raw[0], date_is_tentative=False)
        else:
            raise WendaoError('invalid_reference_date')
    apply_reference_date(spec, now, text=event.content)
    return spec


def attach_answer(service, event, data, *, targets=None, dates=None, timeout=90):
    if data.get('wendao_processed'):
        return
    date_spec = data.setdefault('provider_dates', reference_dates(event))
    validated_dates = data.get('dates', {})
    if validated_dates.get('visit_start_date'):
        start = validated_dates['visit_start_date']
        finish = validated_dates.get('visit_end_date') or start
        date_spec.update(start_date=start, end_date=finish, date_is_tentative=bool(validated_dates.get('date_is_tentative')),
                         day_count=(datetime.fromisoformat(finish)-datetime.fromisoformat(start)).days + 1)
    semantics = ('用户明确要求不填写日历日期，只按第几天参考。' if not date_spec.get('start_date') else
                 ('实际出发日尚未确定，以下仅按' if date_spec.get('date_is_tentative') else '用户指定日期参考：')
                 + date_spec['start_date'] + '起连续' + str(date_spec['day_count']) + '天。')
    target_note = ('；本批仅查询：' + '、'.join(t['name'] + '（' + ((dates or {}).get(t.get('day_index')) or '游览日未明确') + f'）[target-{i+1}]'
                    for i,t in enumerate(targets))) if targets else ''
    if targets:
        data['provider_requested_target_ids'] = [f'target-{i+1}' for i in range(len(targets))]
    query = (event.content[:2000] + target_note + '\n' + semantics +
        '\n仅返回旅行参考，不执行订票、预约、保存或提醒。每个景点使用独立的### <完整名称>标题，'
        '若给出了目标编号，请在对应章节保留编号；返回城市、地址或对象不一致时不得自行替换为其他景点。'
        '分别列出介绍、开放时间、普通票价、预约要求、游玩时长、玩法建议。'
        '不同票种/人群分开；注明规则适用日期和来源，未知不补全。')
    result = service.wendao.query(event, service.store.research, query, timeout=timeout)
    data['wendao_call'] = {k: v for k, v in result.items() if k != 'text'}
    data.setdefault('request_counts', {})['wendao'] = result['physical_calls']
    if result['status'] != 'completed':
        data['wendao_processed'] = True
        return
    source = {'id': max((s['id'] for s in data['sources']), default=0) + 1,
              'type': 'provider_answer', 'provider': 'ctrip_wendao', 'title': '携程问道参考',
              'status': 'provider_unverified', 'text': result['text'], 'url': '',
              'retrieved_at': result['retrieved_at'], 'content_hash': result['content_hash']}
    data['sources'].append(source)
    if targets is None:
        targets = answer_targets(source['text'])
        data['provider_targets'] = targets
    if dates is None:
        dates = {i + 1: (datetime.fromisoformat(date_spec['start_date']) + timedelta(days=i)).date().isoformat()
                 for i in range(date_spec['day_count'])} if date_spec.get('start_date') else {}
    city = data.get('target_scope', {}).get('city') or data.get('destination', '')
    converted = extract_candidates(source, targets, dates, city=city)
    data.setdefault('facts', []).extend(converted.pop('facts'))
    data.update(converted, fact_schema=VERSION, wendao_processed=True)
    data['provider_coverage'] = {'candidate_count': len(data['facts']), 'target_count': len(targets),
                                 'official_verified_count': 0, 'hourly_validation': 'not_performed'}
    resolve_rules(data['facts'])
    service.store.research.save(event, 'reading', data)


def answer_report(data, *, detailed=False):
    if any(s.get('provider')=='ctrip_wendao' for s in data.get('sources', [])):
        from core.travel_answer import presentation, render
        return render(presentation(data), detailed=detailed)
    lines = ['## 携程问道参考', '供应商综合回答，尚未独立核实；不计入官方核验。小时级时间与交通衔接尚未校验。']
    dates = data.get('provider_dates', {})
    if dates.get('start_date'):
        lines.append(('日期暂定，实际出发日尚未确定：' if dates.get('date_is_tentative') else '参考日期：') + dates['start_date']
                     + '起；改期后开放、价格和预约条件需重新核对。')
    call = data.get('wendao_call', {})
    calls = call.get('physical_calls')
    lines.append(f"物理调用：{calls if calls is not None else '未知（一次提交额度已占用）'}；耗时：{call.get('elapsed_ms', '未知')}毫秒；费用与Token用量未知。")
    sources = [s for s in data.get('sources', []) if s.get('provider') == 'ctrip_wendao']
    if not sources:
        lines.append('问道状态：' + call.get('status', 'unavailable') + '；' + call.get('error_code', '未取得完整回答') + '。未自动重发。')
    if detailed:
        for source in sources:
            lines.extend(['\n### 携程问道完整回答', f"来源[{source['id']}]；接收时间：{source['retrieved_at']}。以下为外部参考原文。", source['text']])
    else:
        materials = data.get('activity_materials', [])
        for material in materials:
            lines.append('\n### ' + material['name'])
            for intro in material['intro'][:1]:
                lines.append(intro['text'] + '〔携程问道参考〕')
            for fact in [f for f in data.get('facts', []) if f['target_id'] == material['target_id']][:3]:
                raw = fact['value_raw']
                if len(raw) <= 300:
                    lines.append('- ' + raw)
        lines.append('\n完整回答与未解析内容可在研究报告展开；查看不会重新调用。')
    return '\n'.join(lines)


def save_answer_views(data):
    """Build both current-run displays from one selection; never writes old records."""
    from core.attraction_answers import has_provider, answer_view
    if has_provider(data):
        view=answer_view(data)
        data.update(answer_schema=view['schema_version'],answer_metrics=view['metrics'],answer_stage=view['answer_stage'],
                    report=answer_report(data,detailed=True),brief_report=answer_report(data))
    else:
        data.update(report=answer_report(data,detailed=True),brief_report=answer_report(data))
