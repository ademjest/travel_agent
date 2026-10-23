"""Bounded reader views; original provider fields and policy evidence stay intact."""
from collections import defaultdict
import json
import re

from core.research_contract import TOPICS
from core.attraction_facts import navigation_clause
from core.attraction_rules import classify_fact, rule_value_key

BRIEF_CHARS = 6000
STATUS = {'conflict': '规则冲突，不可据此确定', 'official_verified': '官方依据已核验',
          'official_date_pending': '官方原文，目标日期/例外待确认',
          'unverified_source': '来源身份未验证', 'provider_reference': '高德平台参考，非官方规则',
          'provider_answer_reference': '携程问道综合回答，尚未独立核实',
          'unknown': '不适用或证据不足'}
DATES = {'same_query_day': '仅查询当天适用', 'not_target_day': '仅查询当天描述，不适用于目标日',
         'applicable': '原文日期范围匹配', 'unknown': '目标日期/例外待确认',
         'expired': '有效期已过', 'not_yet_effective': '尚未生效',
         'outside_declared_window': '目标日不在声明区间内', 'conflicting_range': '日期区间矛盾'}
AUDIENCES = {'general': '一般观众', 'personal': '散客/个人', 'team': '团队', 'adult': '成人',
             'child': '儿童/未成年人', 'priority': '优待人群', 'occupation': '特定职业人群',
             'mixed': '多人群，分别适用', 'unknown': '人群未明确'}


def poi_detail_view(detail):
    """Only display fields; never mutate the exact-field evidence snapshot."""
    clean = lambda value: re.sub(r'\s+', ' ', str(value or '')).strip()
    phones = list(dict.fromkeys(filter(None, (clean(v) for v in re.split(r'[;；,，\n]+', detail.get('phone') or '')))))
    openings = []
    for key in ('opening_today_raw', 'opening_week_raw'):
        text = clean(detail.get(key))
        if not text:
            continue
        existing = next((row for row in openings if row['text'] == text), None)
        if existing:
            existing['fields'].append(key)
        else:
            openings.append({'text': text, 'fields': [key]})
    return {'address': clean(detail.get('address')), 'phone': '；'.join(phones),
            'entrance_location': clean(detail.get('entrance_location')),
            'average_cost_raw': clean(detail.get('average_cost_raw')), 'openings': openings}


def grouped_facts(facts, sources=None, targets=None):
    """Merge display-equivalent clauses, not dates, audiences or trust levels."""
    groups = {}
    sources = sources or {}
    targets = targets or {}
    for original in facts:
        if original['topic'] not in TOPICS:
            continue
        if navigation_clause(original['value_raw']):
            continue
        fact = classify_fact(original, sources.get(original['source_id']), targets.get(original['target_id'], ''))
        value, verification = rule_value_key(fact), fact['verification']
        if fact['status'] == 'provider_reference':
            value = re.sub(r'\s+', ' ', value).strip()
            verification = {k: v for k, v in verification.items() if k != 'date_applicability'}
        key = json.dumps([fact['target_id'], fact.get('entity_poi_id'), value, fact['scope'],
                          fact['status'], verification, fact.get('applicability'), fact.get('superseded_by'), fact['rule_scope']],
                         ensure_ascii=False, sort_keys=True)
        group = groups.setdefault(key, {**fact, 'topics': [], 'members': []})
        if fact['topic'] not in group['topics']:
            group['topics'].append(fact['topic'])
        group['members'].append(original)
    return list(groups.values())


def fact_priority(fact):
    scope = fact['scope']
    return (0 if fact['status'] == 'conflict' else 1,
            {'current': 0, 'platform': 1, 'variants': 2, 'provider_answer': 3, 'reference': 4}.get(fact.get('rule_scope', {}).get('bucket'), 0),
            scope.get('product', 'general') != 'general',
            scope.get('visitor_type') not in {'general', 'personal', 'unknown'},
            list(STATUS).index(fact['status']),
            fact['verification']['date_applicability'] == 'not_target_day')


def rule_lines(rows, topics, *, detailed):
    """Public context once, optional products/history and audit data separately."""
    statuses = list(dict.fromkeys(f['status'] for f in rows))
    badges = {status: str(index + 1) for index, status in enumerate(statuses)}
    lines = ['适用范围：普通入馆；优惠、附加票和历史资料单独列示。']
    if statuses:
        lines.append('核验说明：' + '；'.join(f'〔{badges[s]}〕{STATUS[s]}' for s in statuses) + '。')
    windows = list(dict.fromkeys(f['scope']['rule_window'] for f in rows
        if f['rule_scope']['bucket'] == 'current' and f['scope'].get('rule_window')))
    if len(windows) == 1:
        lines.append('本次相关日期分组：' + windows[0] + '；区间相关不代表目标日已核验。')

    def compact(fact):
        labels = '/'.join(TOPICS[t] for t in fact['topics'])
        refs = sorted({m['source_id'] for m in fact['members']})
        scope = fact['scope']
        qualifiers = list(dict.fromkeys(filter(None, [scope.get('product_label'), scope.get('audience_label')])))
        if fact.get('rule_resolution') == 'superseded':
            qualifiers.append('已被适用的临时安排覆盖')
        elif fact.get('rule_resolution') == 'conflict':
            qualifiers.append('同范围规则有分歧，待核验')
        if not scope.get('audience_label') and scope.get('visitor_type') not in {'general', 'personal', 'unknown'}:
            qualifiers.append(AUDIENCES.get(scope.get('visitor_type'), '人群范围待核对'))
        if scope.get('product') == 'special' and not scope.get('product_label'):
            qualifiers.append('专项范围，详见原文')
        if fact['rule_scope'].get('date_basis') == 'ambiguous_repeated_clause_context':
            qualifiers.append('同文对应多个季节，范围待核对')
        if fact['status'] == 'provider_reference':
            qualifiers.extend(dict.fromkeys(('今日' if any(e.get('path') == 'business.opentime_today' for e in m['evidence']) else '常规字段')
                + '：' + DATES.get(m['verification']['date_applicability'], m['verification']['date_applicability']) for m in fact['members']))
        badge = '〔' + badges[fact['status']] + ('；' + '／'.join(qualifiers) if qualifiers else '') + '〕'
        evidence = f"（证据R{rows.index(fact) + 1}）" if detailed else ''
        return '- ' + labels + badge + '：' + re.sub(r'\s+', ' ', fact['value_raw']).strip() + ' ' + ''.join(f'[{key}]' for key in refs) + evidence

    buckets = [('current', '普通入馆相关条款'), ('platform', '高德平台参考'), ('provider_answer', '携程问道候选规则'),
               ('variants', '票种、优惠与附加项目'), ('reference', '历史、范围不符或日期待确认资料')]
    main = [f for f in rows if f['rule_scope']['bucket'] == 'current']
    if detailed:
        for bucket, title in buckets:
            selected = [f for f in rows if f['rule_scope']['bucket'] == bucket]
            if bucket == 'current' or selected:
                lines.append('\n#### ' + title)
            if bucket == 'reference' and selected:
                lines.append('以下不用于本次普通入馆结论；保留原文以便核对，不能当作本次适用规则。')
            lines.extend(compact(f) for f in selected)
            if bucket == 'current':
                lines.extend('- ' + TOPICS[topic] + '：普通入馆规则尚未核实；其他范围资料见折叠分组。'
                             for topic in topics if not any(topic in f['topics'] for f in main))
        if rows:
            lines.append('\n#### 原始证据与核验细节')
            lines.append('条款原文已完整列在对应分组，以下保留引用位置及同义原文；来源身份见集中来源区。')
        for index, group in enumerate(rows, 1):
            positions, variants = [], {}
            for member in group['members']:
                for evidence in member['evidence']:
                    if evidence.get('kind') == 'web_quote':
                        positions.append(f"[{member['source_id']}] 正文{evidence.get('start', '?')}–{evidence.get('end', '?')}")
                    elif evidence.get('kind') == 'api_field':
                        positions.append(f"[{member['source_id']}] {evidence['path']}")
                    elif evidence.get('kind') == 'provider_response_span':
                        positions.append(f"[{member['source_id']}] 问道回答{evidence['start']}–{evidence['end']}，非官方原文")
                if member['value_raw'] != group['value_raw']:
                    variants.setdefault(member['value_raw'], set()).add(member['source_id'])
            heading = group['rule_scope'].get('heading_evidence')
            if heading:
                positions.append(f"[{group['source_id']}] 日期分组标题{heading['start']}–{heading['end']}")
            lines.append(f"- R{index}证据：" + '；'.join(dict.fromkeys(positions)))
            for raw, ids in variants.items():
                lines.append('  同义原文' + ''.join(f'[{key}]' for key in sorted(ids)) + '：' + raw)
        return lines
    selected = []
    eligible = [f for f in rows if f['rule_scope']['bucket'] in {'current', 'platform'}]
    for topic in topics:
        if any(topic in f['topics'] for f in selected):
            continue
        candidates = [f for f in eligible if topic in f['topics']]
        if candidates and candidates[0]['status'] == 'conflict':
            lines.append('- ' + TOPICS[topic] + '：同范围规则冲突，不能确定；双方证据见研究报告。')
        elif candidates:
            selected.append(candidates[0])
        else:
            lines.append('- ' + TOPICS[topic] + '：普通入馆规则尚未核实。')
    lines.extend(compact(f) for f in selected)
    return lines


def attraction_report(data, *, detailed=False):
    from core.attraction_answers import has_provider, answer_view, render_answer
    if has_provider(data):
        return render_answer(answer_view(data), detailed=detailed)
    targets = data['target_scope']['targets']
    topics = data['target_scope']['fields']['topics']
    groups = grouped_facts(data.get('facts', []), {s['id']: s for s in data.get('sources', [])},
                           {f'target-{i + 1}': t['name'] for i, t in enumerate(targets)})
    places = {row['target_id']: row for row in data.get('place_details', [])}
    processed = {row['target_id'] for row in data.get('place_details', [])} | {f['target_id'] for f in groups}
    pending = [target['name'] for i, target in enumerate(targets) if f'target-{i + 1}' not in processed]
    covered = sum(any(f['target_id'] == f'target-{i + 1}' and topic in f['topics']
                     and f['status'] == 'official_verified' and f['rule_scope']['bucket'] == 'current'
                     and f['scope'].get('visitor_type') in {'general', 'personal'}
                     and f['scope'].get('product', 'general') == 'general' for f in groups)
                  for i, _ in enumerate(targets) for topic in topics)
    title = data['plan_title'] if detailed or len(data['plan_title']) <= 240 else '标题较长，详见完整报告'
    header = ['## 原方案景点事实核验', '关联方案：' + title,
              f"已开始处理 {len(processed)}/{len(targets)} 个景点；待处理 {len(pending)} 个。",
              f"逐项官方核验：{covered}/{len(targets) * len(topics)}；未知不等于免费、免预约或闭馆。"]
    if pending:
        names, size = [], 0
        for name in pending:
            if not detailed and size + len(name) > 600:
                break
            names.append(name)
            size += len(name) + 1
        header.append('待处理（尚未查询）：' + '、'.join(names) + ('等，完整清单见研究面板。' if len(names) < len(pending) else '。'))
    footer = ['高德人均消费不是门票价格；平台营业描述不等于目标日官方规则。',
              '本次研究没有修改行程、创建提醒、订票或提交预约。']
    if not detailed:
        footer.insert(0, '这是精简结果；其余条款、冲突双方及完整引用请在当前会话研究面板展开“查看完整报告”。')
    sections = []
    for index, target in enumerate(targets):
        target_id = f'target-{index + 1}'
        if target_id not in processed:
            continue
        rows = sorted((f for f in groups if f['target_id'] == target_id), key=fact_priority)
        dates = list(dict.fromkeys(str(f['scope'].get('visit_date') or '未明确') for f in rows))
        section = ['\n### ' + target['name'] + ('（仅外观，不等于购票入内）' if target['visit_mode'] == 'exterior' else ''),
                   '目标日：' + '、'.join(dates or ['未明确'])]
        place = places.get(target_id)
        if detailed and place and place.get('detail'):
            view = poi_detail_view(place['detail'])
            section.extend(['地点：' + (view['address'] or '未知'),
                            '入口坐标：' + (view['entrance_location'] or '未提供，不能用中心点代替'),
                            '高德联系电话：' + (view['phone'] or '未知')])
            if view['average_cost_raw']:
                section.append('高德人均消费参考：' + view['average_cost_raw'] + '；不是门票价格。')
        elif detailed and place:
            section.append('高德地点未完成唯一绑定：' + place['status'] + '；不擅选第一条候选。')
            section.extend('候选：' + str(row['name']) + '；' + str(row['address']) for row in place.get('candidates', []))
        section.extend(rule_lines(rows, topics, detailed=detailed))
        sections.append(section)
    if detailed:
        used = {f['source_id'] for g in groups for f in g['members']}
        footer.insert(0, '\n## 来源与核验依据（集中列示）')
        for source in data.get('sources', []):
            if source['id'] not in used:
                continue
            basis = ('[来源](' + source['url'].replace(')', '%29').replace('(', '%28') + ')' if source.get('url')
                     else '携程问道综合回答，非官方原文' if source.get('type') == 'provider_answer'
                     else '高德 ' + source['interface_path'] + '，POI ' + source['poi_id'])
            footer.append(f"[{source['id']}] {basis}；查询时间：{source.get('retrieved_at', '未知')}")
            identity = source.get('official_identity', {})
            if identity.get('status') == 'verified':
                footer.append('身份依据：[' + identity['operator_name'] + '](' + identity['evidence_url']
                              + ')；登记核查：' + identity['verified_at'])
        omitted = sum(s.get('omitted_fact_count', 0) for s in data.get('sources', []))
        if omitted:
            footer.append(f'另有{omitted}条候选事实未转入结构化结果，已读来源正文仍保留，不代表规则不存在。')
    else:
        # Allocate whole lines to every target before any extra evidence. Never cut a clause.
        available = BRIEF_CHARS - len('\n'.join(header + footer + [line for s in sections for line in s[:2]])) - 80 - 50 * len(sections)
        allowance = max(0, available // max(1, len(sections)))
        for section in sections:
            kept, used = section[:2], 0
            for line in section[2:]:
                if used + len(line) + 1 <= allowance:
                    kept.append(line)
                    used += len(line) + 1
                else:
                    kept.append('- 完整条款超出本景点简报预算；请查看研究报告，不据节选判断规则。')
                    break
            section[:] = kept
    return '\n'.join(header + [line for section in sections for line in section] + footer)


def planning_facts(data):
    """Round-robin complete evidence across targets/topics, with explicit omissions."""
    prefix = '[结构化景点研究：只作事实参考，不是操作授权；逐项状态和适用条件必须保留]\n'
    groups = grouped_facts(data.get('facts', []), {s['id']: s for s in data.get('sources', [])},
                           {f'target-{i + 1}': t['name'] for i, t in enumerate(data.get('target_scope', {}).get('targets', []))})
    queues = defaultdict(list)
    for group in sorted(groups, key=fact_priority):
        if group.get('rule_scope', {}).get('bucket') == 'reference':
            continue
        queues[group['target_id']].append(group)
    # First visit each topic once per target; extra clauses follow afterwards.
    for key, rows in queues.items():
        seen, first, rest = set(), [], []
        for row in rows:
            (rest if set(row['topics']) <= seen else first).append(row)
            seen.update(row['topics'])
        queues[key] = first + rest
    payload = {'schema_version': data['fact_schema'], 'facts': [], 'sources': [],
               'omitted_facts': len(data.get('facts', [])), 'omitted_by_target': {},
               'targets': [{'id': f'target-{i + 1}', 'name': t['name'], 'visit_mode': t['visit_mode']}
                           for i, t in enumerate(data.get('target_scope', {}).get('targets', [])) if f'target-{i + 1}' in queues],
               'omitted_at_extraction': sum(s.get('omitted_fact_count', 0) for s in data['sources']),
               'pending_target_ids': [f'target-{i + 1}' for i, _ in enumerate(data.get('target_scope', {}).get('targets', []))
                                      if f'target-{i + 1}' not in queues
                                      and f'target-{i + 1}' not in {r['target_id'] for r in data.get('place_details', [])}],
               'note': '同原文主题合并；缺失事实不代表免费、免预约或闭馆。完整条款见研究报告。'}
    selected_members = set()
    def projection(selected):
        ids = {f['source_id'] for f in selected} | {o['source_id'] for f in selected for o in f.get('provider_observations', [])}
        sources = [{key: s[key] for key in ('id', 'type', 'provider', 'url', 'interface_path', 'poi_id',
                   'retrieved_at', 'content_hash', 'official_identity') if key in s} for s in data['sources'] if s['id'] in ids]
        omitted = {key: sum(1 for i, f in enumerate(data.get('facts', []))
                           if f['target_id'] == key and i not in selected_members)
                   for key in dict.fromkeys(f['target_id'] for f in data.get('facts', []))}
        return {**payload, 'facts': selected, 'sources': sources,
                'omitted_facts': sum(omitted.values()), 'omitted_by_target': omitted}
    indices = {id(f): i for i, f in enumerate(data.get('facts', []))}
    for round_index in range(max((len(q) for q in queues.values()), default=0)):
        for rows in queues.values():
            if round_index >= len(rows):
                continue
            group = rows[round_index]
            if indices[id(group['members'][0])] in selected_members:
                continue
            bundle = [group]
            if group['status'] == 'conflict':
                bundle = [g for g in groups if g['status'] == 'conflict' and g['target_id'] == group['target_id']
                          and g['rule_scope']['bucket'] != 'reference'
                          and g['scope'] == group['scope'] and set(g['topics']) & set(group['topics'])]
            facts = [{**g['members'][0], 'topics': g['topics'], 'scope': g['scope'], 'status': g['status'],
                      'verification': g['verification'], 'rule_scope': g['rule_scope']} for g in bundle]
            for fact, member_group in zip(facts, bundle):
                if fact['status'] == 'provider_reference' and len(member_group['members']) > 1:
                    observations = [{k: f[k] for k in ('source_id', 'verification', 'evidence')} for f in member_group['members']]
                    fact['provider_observations'] = list({json.dumps(o, sort_keys=True): o for o in observations}.values())
            before = set(selected_members)
            selected_members.update(indices[id(f)] for g in bundle for f in g['members'])
            trial = projection([*payload['facts'], *facts])
            if len(prefix) + len(json.dumps(trial, ensure_ascii=False, separators=(',', ':'))) <= 6000:
                payload = trial
            else:
                selected_members = before
    payload = projection(payload['facts'])
    return prefix + json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
