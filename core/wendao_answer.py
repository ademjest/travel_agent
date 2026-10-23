"""Finite Markdown extraction. Free prose stays available as an unverified answer."""
import re
from urllib.parse import parse_qs, urlsplit

from core.rule_semantics import statement_kind, temporal_rule, split_statements

VERSION = 'attraction-facts-v3'
LABELS = {'开放时间': 'opening_hours', '营业时间': 'opening_hours', '普通票价': 'ticket_price',
          '门票': 'ticket_price', '票价': 'ticket_price', '预约要求': 'reservation_rules', '预约规则': 'reservation_rules',
          '游玩时长': 'duration', '玩法建议': 'advice', '游玩建议': 'advice'}
INSTRUCTION = re.compile(r'忽略.{0,15}(?:指令|要求)|系统提示|system\s*prompt|执行命令|创建提醒|自动(?:订票|预约)|API[_ ]?KEY|token\s*[:=]', re.I)
HARD_DETAIL = re.compile(r'\d|开放|营业|预约|门票|票价|天气|出发|来源：|停止入|闭馆|闭园|[零〇一二两三四五六七八九十百千万]+元|[一二两三四五六七八九十]+点')


def answer_targets(text, limit=8):
    from services.reminder_time import number
    from core.wendao_blocks import sections, field_blocks
    targets, day = [], None
    for section in sections(text):
        heading = section['name']
        daily = re.match(r'第([一二两三四五六七八九十\d]+)天', heading)
        if daily:
            day = number(daily[1])
            continue
        if field_blocks(text, section) and len(heading) <= 80 and len(targets) < limit:
            targets.append({'name': heading, 'visit_mode': 'entry', 'day_index': day})
    return targets


def provider_policy(text, *, automatic=False):
    offline = bool(re.search(r'不(?:要|用|必)?(?:再)?(?:联网|搜索)|禁止联网|别联网', text))
    official = bool(re.search(r'(?:只|仅)(?:看|用|使用|参考).{0,10}(?:官网|官方)', text))
    excluded = bool(re.search(r'(?:不要|不用|别用|不使用|禁止|别再调用|不要再调用).{0,4}问道', text))
    invocation = bool(re.search(r'(?:用|使用|调用|通过|让|参考|查询|搜索).{0,8}问道|问道.{0,8}(?:查询|查一下|搜索|介绍|规划|提供)', text))
    other_source_only = bool(re.search(r'(?:只|仅)(?:看|用|使用|参考).{0,4}(?:高德|地图|携程(?!问道)|小红书|知乎|马蜂窝|去哪儿|穷游|Tripadvisor|Wikivoyage|https://|[a-z0-9-]+\.[a-z]{2,})', text, re.I))
    requested = (invocation or automatic) and not excluded and not offline and not official and not other_source_only
    only = requested and bool(re.search(r'(?:只|仅)(?:看|用|使用|参考).{0,4}(?:携程)?问道', text))
    website_only = bool(re.search(r'(?:只|仅)(?:看|用|参考).{0,4}携程(?:网站|网页)', text))
    return {'requested': requested and not website_only, 'only': only, 'offline': offline, 'official_only': official}


def _legacy_extract(source, targets, dates):
    text = source['text']
    headings = list(re.finditer(r'^#{2,4}\s+<?([^\n<>]+)>?\s*$', text, re.M))
    facts, materials, mappings = [], [], []
    for index, target in enumerate(targets):
        target_id = f'target-{index + 1}'
        matching = [(i, h) for i, h in enumerate(headings) if h[1].strip(' <>*') == target['name']]
        # Repeated sections and near-name matches must be resolved explicitly.
        if len(matching) != 1:
            continue
        i, heading = matching[0]
        start = heading.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section = text[start:end]
        visit_date = dates.get(target.get('day_index', 1), '')
        spans = []
        for match in re.finditer(r'^\s*[-*]\s+\*\*([^*\n]+)\*\*\s*[：:]\s*([^\n]+)', section, re.M):
            label, raw = match[1].strip('：: '), match[2].strip()
            topic = LABELS.get(label)
            if topic is None or INSTRUCTION.search(raw):
                continue
            offset = start + match.start(2) + len(match[2]) - len(match[2].lstrip())
            spans.append((topic, raw, offset))
        for topic, raw, offset in spans:
            product = 'mixed' if re.search(r'演出|套餐|VIP|体验|餐游|观光车', raw) else 'admission'
            audience = 'mixed' if len(re.findall(r'成人|儿童|老人|老年|军人', raw)) > 1 else 'adult' if '成人' in raw else 'unknown'
            kind = 'advice' if topic == 'advice' else statement_kind(raw) if topic == 'duration' else 'rule'
            role = ('mixed' if re.search(r'停止|清园|闭园|闭馆', raw) and re.search(r'\d{1,2}:\d{2}\s*[-—]', raw)
                    else 'last_entry' if re.search(r'停止入|最晚入', raw) else 'opening' if topic == 'opening_hours' else None)
            value = {'schema_version': VERSION, 'target_id': target_id, 'entity_poi_id': None, 'topic': topic,
                'value_raw': raw, 'source_id': source['id'], 'statement_kind': kind,
                'scope': {'visit_date': visit_date or None, 'visit_mode': target.get('visit_mode', 'entry'),
                          'visitor_type': audience, 'product': 'special' if product != 'admission' else 'general',
                          'product_kind': product, 'product_label': raw if product == 'mixed' else '', 'time_role': role},
                'evidence': [{'kind': 'provider_response_span', 'quote': raw, 'start': offset, 'end': offset + len(raw)}],
                'verification': {'identity': 'provider', 'entity_match': 'pending', 'content_support': 'exact_provider_span',
                                 'date_applicability': 'unknown'},
                'status': 'provider_answer_reference', 'extraction_status': 'unresolved_product' if product == 'mixed' else 'candidate',
                'temporal_rule': temporal_rule(raw, visit_date),
                'rule_scope': {'bucket': 'provider_answer', 'date_relevance': 'unknown'}}
            value['statements'] = split_statements(raw)
            if topic == 'ticket_price':
                value['product_candidates'] = [{'raw': m[0], 'start': offset + m.start(), 'end': offset + m.end(),
                    'amounts': re.findall(r'(\d+(?:\.\d+)?)\s*元', m[0]),
                    'pricing_unit': next(iter(re.findall(r'元\s*[/／]\s*(人|张|份|船|套)', m[0])), None)}
                    for m in re.finditer(r'[^；;。]+[；;。]?', raw)]
            facts.append(value)
        intro = []
        for match in re.finditer(r'[^\n]+', section):
            line = match[0].strip()
            if not line or line.startswith(('-', '*', '#', '[')):
                continue
            # Keep complete sentences; policy, money, times and instructions are not introductions.
            for sentence in re.findall(r'[^。！？]+[。！？]?', line):
                if re.search(r'\d', sentence) and not INSTRUCTION.search(sentence):
                    # A complete descriptive suffix can remain usable when an earlier
                    # comma clause contains an unstructured measurement or historical year.
                    suffix = []
                    for clause in re.findall(r'[^，]+[，]?', sentence):
                        if HARD_DETAIL.search(clause):
                            suffix = []
                        else:
                            suffix.append(clause)
                    sentence = ''.join(suffix)
                if (not INSTRUCTION.search(sentence) and not HARD_DETAIL.search(sentence)
                        and 8 <= len(sentence) <= 150):
                    position = text.find(sentence, start, end)
                    intro.append({'text': sentence, 'source_ids': [source['id']], 'start': position, 'end': position + len(sentence)})
            break
        tips = [{'text': raw, 'source_ids': [source['id']], 'kind': 'advice', 'start': pos, 'end': pos + len(raw)}
                for topic, raw, pos in spans if topic == 'advice' and len(raw) <= 120
                and not HARD_DETAIL.search(raw) and not re.search(r'必须|不得|须', raw)]
        if intro:
            # Source-grounded general experience; no new venue or hard numeric claim.
            tips = tips or [{'text': '按兴趣选择体验内容，并给中途休息留出余地。', 'source_ids': [], 'kind': 'advice'}]
        materials.append({'target_id': target_id, 'name': target['name'], 'intro': intro[:2], 'tips': tips[:2],
                          'source_ids': [source['id']], 'entity_poi_id': None})
        for link in re.findall(r'https://[^\s<>\)]+', section):
            parsed = urlsplit(link)
            if parsed.hostname and (parsed.hostname == 'ctrip.com' or parsed.hostname.endswith('.ctrip.com')):
                params = parse_qs(parsed.query)
                if params.get('poiId'):
                    mappings.append({'target_id': target_id, 'ctrip_poi_id': params['poiId'][0],
                                     'poi_type': params.get('poiType', [None])[0], 'amap_poi_id': None,
                                     'basis': 'provider_section_link', 'status': 'unbound'})
    for fact in facts:
        for span in fact['evidence']:
            if text[span['start']:span['end']] != span['quote']:
                raise ValueError('provider_span_mismatch')
    return {'facts': facts, 'activity_materials': materials, 'provider_mappings': mappings,
            'unparsed_answer_retained': True}


def extract_candidates(source, targets, dates, *, city='', aliases=None):
    from core.wendao_blocks import bind_sections, extract_blocks
    bindings = bind_sections(source, targets, city, aliases)
    renamed = [{**target, 'name':binding['sections'][0]['name'] if binding['status']=='bound' else '__unbound__'+str(i)}
               for i,(target,binding) in enumerate(zip(targets,bindings))]
    legacy = _legacy_extract(source, renamed, dates)
    for row in legacy['activity_materials']:
        index = int(row['target_id'].split('-')[1])-1
        row['name'] = targets[index]['name']
    existing = {row['target_id'] for row in legacy['activity_materials']}
    for target,binding in zip(targets,bindings):
        if binding['status']!='bound' or binding['target_id'] in existing:
            continue
        part=binding['sections'][0]
        prefix='### '+target['name']+'\n'
        temporary={**source,'text':prefix+source['text'][part['start']:part['end']]}
        recovered=_legacy_extract(temporary,[target],dates)['activity_materials']
        for row in recovered:
            row['target_id']=binding['target_id']
            for item in row['intro']+row['tips']:
                if 'start' in item:
                    item['start']+=part['start']-len(prefix)
                    item['end']+=part['start']-len(prefix)
            legacy['activity_materials'].append(row)
    facts, retained = extract_blocks(source, targets, dates, bindings)
    # Preserve complete cultural paragraphs, including historical numbers. Policy
    # prose remains in typed facts, rather than being smuggled into introductions.
    from core.wendao_blocks import field_blocks
    for target, binding in zip(targets, bindings):
        if binding['status'] != 'bound':
            continue
        part = binding['sections'][0]
        blocks = field_blocks(source['text'], part)
        finish = min((b['context_start'] for b in blocks), default=part['end'])
        intro = []
        for match in re.finditer(r'[^\n]+', source['text'][part['start']:finish]):
            raw = match[0].strip()
            if (8 <= len(raw) <= 1600 and not raw.startswith(('-', '*', '#', '[', '|'))
                    and not INSTRUCTION.search(raw) and not re.search(r'开放时间|票价|预约|\d+\s*元|\d{1,2}:\d{2}', raw)):
                start = part['start']+match.start()+len(match[0])-len(match[0].lstrip())
                intro.append({'text':raw, 'source_ids':[source['id']], 'start':start, 'end':start+len(raw)})
        row = next((m for m in legacy['activity_materials'] if m['target_id']==binding['target_id']), None)
        if row is None:
            row = {'target_id':binding['target_id'], 'name':target['name'], 'intro':[], 'tips':[],
                   'source_ids':[source['id']], 'entity_poi_id':None}
            legacy['activity_materials'].append(row)
        if intro:
            row['intro'] = intro
        tips = [b for b in blocks if b['topic']=='advice']
        row['tips'] = [{'text':source['text'][b['start']:b['end']], 'source_ids':[source['id']],
                        'kind':'advice', 'start':b['start'], 'end':b['end']} for b in tips
                       if b['end']-b['start']<=1800 and not INSTRUCTION.search(source['text'][b['start']:b['end']])] or row['tips']
    return {**legacy, 'facts':facts, 'answer_sections':retained}
