"""Pure source parsing, target/date projection and provider selection for research."""
import re
import json
from urllib.parse import parse_qs, urlsplit
from core.date_rules import date_window, policy_date, temporal_rule
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from tools.trip.constraints import apply_reference_date, explicitly_undated
from core.time_input import number, parse_reminder_time, BEIJING, NUMBER
from tools.trip.constraints import date_range
from infrastructure.wendao_client import WendaoError
from core.data_paths import PROJECT_ROOT

EXTRACTION_VERSION = 'wendao-markdown-v3'
EXTRACTOR_REVISION = 'wendao-sections-v8'
LEGACY_EXTRACTION_VERSION = 'wendao-markdown-v2'

VERSION = 'attraction-facts-v3'


LEGACY_LABELS = {'开放时间': 'opening_hours', '营业时间': 'opening_hours', '普通票价': 'ticket_price',
          '门票': 'ticket_price', '票价': 'ticket_price', '预约要求': 'reservation_rules', '预约规则': 'reservation_rules',
          '游玩时长': 'duration', '玩法建议': 'advice', '游玩建议': 'advice'}


INSTRUCTION = re.compile(r'忽略.{0,15}(?:指令|要求)|系统提示|system\s*prompt|执行命令|创建提醒|自动(?:订票|预约)|API[_ ]?KEY|token\s*[:=]', re.I)


HARD_DETAIL = re.compile(r'\d|开放|营业|预约|门票|票价|天气|出发|来源：|停止入|闭馆|闭园|[零〇一二两三四五六七八九十百千万]+元|[一二两三四五六七八九十]+点')


PRACTICAL = re.compile(r'开放|营业|开门|闭馆|闭园|停止入|停止售|预约|购票|门票|票价|免费|元[/／人张套]|\d+\s*元|\d{1,2}[:：]\d{2}|无需|必须|仅限|登塔|演出|花期|展期|官方(?:已|确认|核实)')
HISTORICAL_OPENING = re.compile(r'\d{3,4}年(?:\d{1,2}月(?:\d{1,2}日)?)?(?:(?:建成|扩建|改建|落成|竣工)(?:后|并)?)?(?:正式|首次|重新)?(?:对外)?(?:开放|开馆|开园)')


def practical_detail(text):
    # Dated opening history is cultural material; schedules and planned openings are policies.
    if re.search(r'(?:将|计划|预计|拟|定于)[^。！？；\n]*(?:开放|开馆|开园)', text):
        return True
    return bool(PRACTICAL.search(HISTORICAL_OPENING.sub('', text)))


DAY_HEADING = re.compile(r'^(?:第([一二两三四五六七八九十\d]+)天|Day\s*(\d+))',re.I)


def section_day(section,structure):
    parents=[p for p in structure if p['start']<=section['heading_start']<p['end']
             and p['level']<section['level'] and DAY_HEADING.match(p['name'])]
    return number(next(v for v in DAY_HEADING.match(parents[-1]['name']).groups() if v)) if parents else None


def section_role(section, structure):
    parents=[p for p in structure if p['start']<=section['heading_start']<p['end'] and p['level']<section['level']]
    for part in reversed([*parents,section]):
        title=re.sub(r'^[一二三四五六七八九十\d]+[、.．]\s*','',part['name'])
        if re.match(r'^[GDCZTKYSL]\d{1,5}(?:\b|[（(])',title):return 'transport'
        if re.fullmatch(r'(?:往返)?(?:交通|铁路|车次|去程|返程)(?:参考|信息|方案|安排|详情)?(?:[（(].*[）)])?',title):return 'transport'
        if re.fullmatch(r'(?:住宿|酒店|餐饮|美食)(?:推荐|建议|信息|安排|参考)',title):return 'commercial'
        if re.fullmatch(r'景点(?:详细)?(?:详情|信息|介绍|资料)卡?',title):return 'detail'
        if DAY_HEADING.match(title) or re.fullmatch(r'(?:逐日)?行程安排|逐日安排',title):return 'schedule'
    return ''


def answer_targets(text, limit=8):
    targets = []
    parts, adopted = sections(text), []
    structure=sections(text,include_fields=True)
    for section in parts:
        heading = section['name']
        if DAY_HEADING.match(heading) or section_role(section,structure) in {'transport','commercial'}:
            continue
        day=section_day(section,structure)
        children = [p for p in parts if section['start']<=p['heading_start']<section['end'] and p['level']>section['level']]
        owned = [b for b in field_blocks(text,section) if b['topic'] not in {'intro','unknown'}
                 and not any(p['heading_start']<=b['context_start']<p['end'] for p in children)]
        if (owned and not any(p['start']<=section['heading_start']<p['end'] for p in adopted)
                and len(heading) <= 80 and len(targets) < limit):
            targets.append({'name': heading, 'visit_mode': 'entry', 'day_index': day})
            adopted.append(section)
    return targets


def planning_policy(spec,text='',*,automatic=False):
    """Intersect saved and current source permissions for each planning dependency."""
    policy=provider_policy(text,automatic=automatic);policies=[policy.copy()]
    sources=[spec,spec.get('research_context',{}),getattr(spec.get('source_context'),'data',{})]
    if isinstance(spec.get('source_context'),dict):sources.append(spec['source_context'])
    for source in sources:
        saved=dict(source.get('provider_policy',{}))
        if source.get('strict_sources') and not saved.get('only') and not saved.get('official_only'):
            domains=source.get('preferred_domains',[])
            saved['other_source_only']=True
            saved['map_only']=bool(saved.get('map_only') or domains and all(d in {'amap.com','restapi.amap.com','高德'} for d in domains))
        policies.append(saved)
    for key in ('offline','only','official_only','other_source_only','source_restricted'):
        policy[key]=any(p.get(key) for p in policies)
    policy['map_allowed']=all(not any(p.get(k) for k in ('offline','only','official_only')) and
        (not p.get('other_source_only') or p.get('map_only')) for p in policies)
    policy['rail_allowed']=all(not any(p.get(k) for k in ('offline','only','official_only','other_source_only')) for p in policies)
    policy['rules_allowed']=policy['rail_allowed'] # Restricted previews keep unresolved reminder rules explicit.
    policy['requested']=policy['requested'] and not policy['source_restricted'] and all(not any(p.get(k) for k in ('offline','official_only','other_source_only')) for p in policies)
    return policy


def provider_policy(text, *, automatic=False):
    offline = bool(re.search(r'不(?:要|用|必)?(?:再)?(?:联网|搜索)|(?:禁止|别)(?:再)?(?:联网|搜索)|'
                            r'(?:只|仅)(?:看|用|使用|参考)(?:已保存|已有|现有)(?:的)?(?:资料|研究|结果)', text))
    official = bool(re.search(r'(?:只|仅)(?:看|用|使用|参考)[^，,；;。！？\n]{0,10}(?:官网|官方)', text))
    excluded = bool(re.search(r'(?:不要|不用|别用|不使用|禁止|别再调用|不要再调用).{0,4}问道', text))
    invocation = bool(re.search(r'(?:用|使用|调用|通过|让|参考|查询|搜索).{0,8}问道|问道.{0,8}(?:查询|查一下|搜索|介绍|规划|提供)', text))
    other_source_only = bool(re.search(r'(?:只|仅)(?:看|用|使用|参考).{0,4}(?:高德|地图|携程(?!问道)|小红书|知乎|马蜂窝|去哪儿|穷游|Tripadvisor|Wikivoyage|https://|[a-z0-9-]+\.[a-z]{2,})', text, re.I))
    requested = (invocation or automatic) and not excluded and not offline and not official and not other_source_only
    only = bool(re.search(r'(?:只|仅)(?:看|用|使用|参考).{0,4}(?:携程)?问道', text)) and not any((excluded, official, other_source_only))
    website_only = bool(re.search(r'(?:只|仅)(?:看|用|参考).{0,4}携程(?:网站|网页)', text))
    return {'requested': requested and not website_only, 'only': only, 'offline': offline, 'official_only': official,
            'other_source_only':other_source_only,
            'map_only':bool(re.search(r'(?:只|仅)(?:看|用|使用|参考).{0,4}(?:高德|地图)',text)),
            'source_restricted':excluded or official or other_source_only,
            'explicit_requested': invocation and not any((excluded, offline, official, other_source_only, website_only))}




FIELD_LABELS = {'介绍':'intro', '景点介绍':'intro', '简介':'intro', '概况':'intro',
          '特色亮点':'intro', '文化介绍':'intro', '景点特色':'intro', '主要看点':'intro', '展陈特色':'intro',
          '开放时间':'opening_hours', '营业时间':'opening_hours', '开园时间':'opening_hours',
          '普通票价':'ticket_price', '门票价格':'ticket_price', '门票票种':'ticket_price', '其他票种':'ticket_price', '门票':'ticket_price', '票价':'ticket_price',
          '优惠政策':'ticket_price', '优待政策':'ticket_price',
          '预约要求':'reservation_rules', '预约方式':'reservation_rules', '预订要求':'reservation_rules',
          '预约规则':'reservation_rules', '票务咨询':'phone', '联系电话':'phone', '咨询电话':'phone', '票务电话':'phone', '总机电话':'phone',
          '推荐游玩时长':'duration', '建议游玩时长':'duration', '游玩时长':'duration', '玩法建议':'advice', '游玩建议':'advice'}


LABEL = '|'.join(sorted(FIELD_LABELS, key=len, reverse=True))


FIELD = re.compile(r'^[ \t]*(?:#{1,6}[ \t]+)?(?:[-+*]\s+|\d+[.)、]\s+)?(?:\*\*)?(?P<label>'+LABEL+
                   r')(?:\*\*)?[ \t]*(?P<context>[（(][^）)\n]*[）)])?(?:\*\*)?[ \t]*[：:][ \t]*(?:\*\*)?')

LIST_ITEM = re.compile(r'^[ \t]*(?:[-+*]|\d+[.)、])[ \t]+')
UNKNOWN_FIELD = re.compile(r'^[ \t]*(?:#{1,6}[ \t]+)?(?:[-+*]\s+|\d+[.)、]\s+)?(?:\*\*)?'
                          r'[^\W\d][\w /／-]{0,28}(?:\*\*)?(?:[（(][^）)\n]*[）)])?(?:\*\*)?[ \t]*[：:]')


HEAD = re.compile(r'^(?:#{1,6}[ \t]+(?P<md>[^\n]+)|\*\*(?P<bold>[^*\n]+)\*\*[ \t]*)$', re.M)
FENCED_BLOCK = re.compile(r'(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)')


BOUNDARY = re.compile(r'入口|出口|停车场|售票处|游客中心|[东西南北]门$')


def clean(text):
    return re.sub(r'[<>*`]', '', text).strip()


def section_title(text):
    """Remove heading enumeration, never numerals inside the entity's name."""
    ordinal=r'[一二两三四五六七八九十百\d]+'
    return re.sub(r'^(?:(?:景点|地点|场馆)\s*'+ordinal+r'[：:、]\s*|第\s*'+ordinal+r'站[：:、]\s*|'
                  +ordinal+r'[.．、)][ \t]+|[（(]'+ordinal+r'[）)][ \t]*)','',clean(text)).strip()


def sections(text, *, include_fields=False):
    title = lambda m: section_title(m.group('md') or m.group('bold'))
    fences=[m.span() for m in FENCED_BLOCK.finditer(text)]
    heads = [m for m in HEAD.finditer(text) if not any(lo<=m.start()<hi for lo,hi in fences)
             and (include_fields or not re.fullmatch('(?:'+LABEL+r')(?:[（(][^）)]*[）)])?[：:]?', title(m)))]
    parts, level = [], 1
    for match in heads:
        if match.group('md'):
            level = len(match[0])-len(match[0].lstrip('#'))
        parts.append({'name':title(match),'start':match.end(),'heading_start':match.start(),'level':level})
    for i,part in enumerate(parts):
        part['end'] = next((p['heading_start'] for p in parts[i+1:] if p['level']<=part['level']),len(text))
    return parts


def bind_sections(source, targets, city='', aliases=None):
    text = source['text']
    city = city.removesuffix('市')
    catalog = json.loads((PROJECT_ROOT/'data/attraction_aliases.json').read_text(encoding='utf-8'))
    catalog += aliases or []
    result = []
    source_sections = sections(text)
    structure=sections(text,include_fields=True)
    for index,target in enumerate(targets):
        name, tid = section_title(target['name']), f'target-{index+1}'
        candidates = []
        for section in source_sections:
            day=section_day(section,structure)
            if day is not None and target.get('day_index') is not None and day!=target['day_index']:continue
            title, body = section['name'], clean(text[section['start']:section['end']])
            declared = re.search(r'(?:位于|城市[：:]\s*)([\u4e00-\u9fff]{2,8}?)市', body)
            if not declared:
                declared = re.search(r'城市[：:]\s*([\u4e00-\u9fff]{2,8}?)(?=[\s，。]|$)',body)
            if city and declared and declared[1] != city:
                continue
            location = re.search(r'(?:位于|地址[：:])([^。\n]+)',body)
            expected = [entry for entry in catalog if city and entry['city'].removesuffix('市')==city
                        and name in [entry['name'],*entry.get('aliases',[])]]
            if location and re.search(r'[路街巷]\d+号',location[1]) and expected and not any(
                    marker in location[1] for entry in expected for marker in entry.get('address_markers',[])):
                continue
            mapped_title = re.sub(r'\[?target-\d+\]?\s*[|｜:：、-]?\s*', '', title).strip()
            basis = 'exact_name' if mapped_title == name else None
            if not basis and city and mapped_title == city+name and (city in body):
                basis = 'city_qualified_name'
            if not basis:
                for entry in catalog:
                    if (city and entry['city'].removesuffix('市') == city and name in [entry['name'],*entry.get('aliases', [])]
                            and mapped_title in [entry['name'],*entry.get('aliases', [])]
                            and city in body and any(a in body for a in entry.get('address_markers', []))):
                        basis = entry.get('basis', 'reviewed_alias')
            if not basis and title == tid and name in body and city and city in body and not BOUNDARY.search(body):
                basis = 'target_id_and_named_city'
            if basis and (not BOUNDARY.search(mapped_title) or mapped_title == name):
                candidates.append({**section, 'basis':basis})
        related=[]
        details=[p for p in candidates if section_role(p,structure)=='detail']
        if len(candidates)>1 and len(details)==1 and all(
                p is details[0] or section_role(p,structure)=='schedule' for p in candidates):
            identities=set();addresses=set()
            for part in candidates:
                body=text[part['start']:part['end']]
                for link in re.findall(r'https://[^\s<>\)]+',body):
                    url=urlsplit(link)
                    if url.hostname and (url.hostname=='ctrip.com' or url.hostname.endswith('.ctrip.com')):
                        identities.update(parse_qs(url.query).get('poiId',[]))
                addresses.update(re.findall(r'(?:地址[：:]|位于)\s*([^。\n]*?[路街巷]\s*\d+号)',clean(body)))
            if len(identities)<=1 and len(addresses)<=1:
                related=[p for p in candidates if p is not details[0]]
                candidates=details
        result.append({'target_id':tid, 'name':name, 'source_id':source['id'],
                       'status':'bound' if len(candidates)==1 else 'ambiguous' if candidates else 'unmatched',
                       'sections':candidates, **({'related_sections':related,'selection_reason':'detail_over_schedule'} if related else {})})
    # One returned chapter cannot silently identify two competing research targets.
    counts = {}
    for row,target in zip(result,targets):
        for part in row['sections']:
            identity=(target.get('poi_id'),target['name'],target.get('visit_mode','entry'))
            counts.setdefault(part['start'],set()).add(identity)
    for row in result:
        if row['status']=='bound' and len(counts[row['sections'][0]['start']])>1:
            row['status']='ambiguous'
    return result


def field_blocks(text, section):
    start, end = section['start'], section['end']
    lines = list(re.finditer(r'[^\n]+(?:\n|$)', text[start:end]))
    starts = []
    fenced = False
    for i,line in enumerate(lines):
        raw = line[0].rstrip('\r\n')
        if raw.strip().startswith(('```','~~~')):
            if not fenced:starts.append((i,'unknown',0,''))
            fenced = not fenced
            continue
        if fenced:continue
        match = FIELD.match(raw)
        if match:
            starts.append((i, FIELD_LABELS[match['label']], match.end(), match.group('context') or ''))
        elif (heading := re.fullmatch(r'('+LABEL+r')([（(][^）)\n]*[）)])?', clean(raw).lstrip('#').strip())):
            starts.append((i, FIELD_LABELS[heading[1]], len(raw), heading[2] or ''))
        elif raw.lstrip().startswith('|'):
            cells = [clean(c) for c in raw.strip().strip('|').split('|')]
            if cells and cells[0] in FIELD_LABELS:
                payload = raw.find('|', raw.find('|')+1)+1
                starts.append((i, FIELD_LABELS[cells[0]], payload, ''))
            elif len(cells)>1 and any('价格' in c or '票价' in c for c in cells) and any('票种' in c for c in cells):
                if not starts or starts[-1][1]!='ticket_price':
                    starts.append((i, 'ticket_price', len(raw), raw))
        elif (unknown := UNKNOWN_FIELD.match(raw)):
            # A child list belongs to its parent field. An unknown peer still
            # ends the previous field; lack of a label mapping is not inheritance.
            parent = lines[starts[-1][0]][0] if starts else ''
            indent = len(raw)-len(raw.lstrip(' \t'))
            parent_indent = len(parent)-len(parent.lstrip(' \t'))
            if starts and (indent > parent_indent or LIST_ITEM.match(raw) and not LIST_ITEM.match(parent)
                           and starts[-1][1] in {'intro','ticket_price','opening_hours','reservation_rules','advice'}):
                continue
            starts.append((i, 'unknown', unknown.end(), ''))
    blocks = []
    for j,(line_index,topic,payload,context) in enumerate(starts):
        line = lines[line_index]
        finish = lines[starts[j+1][0]].start()+start if j+1<len(starts) else end
        link = re.search(r'(?m)^\s*[-*]?\s*\[(?:预定|预订|来源)', text[start+line.start():finish])
        if link:
            finish = min(finish, start+line.start()+link.start())
        body_start = start+line.start()+payload
        if not text[body_start:start+line.end()].strip():
            body_start = start+line.end()
        while body_start<finish and text[body_start].isspace():
            body_start+=1
        while finish>body_start and text[finish-1].isspace():
            finish-=1
        if body_start>=finish and topic not in {'intro','unknown'}:
            continue
        prefix_start = start+line.start()
        # Ticket rows inherit the entire block heading, including declared date/source.
        blocks.append({'topic':topic,'start':body_start,'end':finish,'context':text[prefix_start:body_start].strip(),
                       'context_start':prefix_start,'context_end':body_start})
    return blocks


def date_check(raw, context, visit_date):
    """Compare declared windows without inventing a year or trusting an applicability claim."""
    result = {'state':'unknown'}
    declared = re.search(r'适用日期\s*[：:]?\s*(20\d{2}-\d{1,2}-\d{1,2})', context)
    if declared:
        result.update(state='target_unknown',declared=declared[1])
    target = policy_date(visit_date)
    if not target:
        return result
    if declared:
        result.update(state='matching' if declared[1]==target.isoformat() else 'outside', declared=declared[1])
    window = date_window(raw, target)
    state = window.state(target)
    # An exception stays unresolved, but cannot make "within this interval" true.
    if (window.exception and window.start and window.end and re.search(r'处于此时段|属于.{0,6}时段', raw)
            and replace(window, exception='').state(target) == 'outside'):
        state = 'outside'
    if state in {'matching', 'outside', 'not_yet_effective'}:
        inside = state == 'matching'
        result.update(state='outside' if not inside or result['state']=='outside' else
                      'regular' if window.basis=='explicit_regular_rule' and not declared else 'matching',
                      declared=result.get('declared', window.recurrence or window.quote))
        if not inside and re.search(r'处于此时段|正常开放|属于.{0,6}时段', raw):
            result['issue']='target_outside_declared_window'
    temporary = temporal_rule(raw,visit_date)
    if temporary['explicit_override'] and temporary['applicability']=='matching' and not (declared and result['state']=='outside'):
        result.update(state='matching',temporary_override=True)
        result.pop('issue',None)
    return result


def extract_blocks(source, targets, dates, bindings):
    facts, retained = [], []
    text = source['text']
    for i,(target,binding) in enumerate(zip(targets,bindings)):
        if binding['status']!='bound':
            retained.append(binding)
            continue
        part = binding['sections'][0]
        blocks = field_blocks(text,part)
        retained.append({**binding,'fields':blocks,
            'processing_status':'parsed' if text[part['start']:part['end']].strip() else 'empty_section'})
        for block in blocks:
            topic, lo, hi = block['topic'], block['start'], block['end']
            if topic in {'intro','unknown'}:
                continue  # Cultural and unclassified blocks are not admission facts.
            spans = list(re.finditer(r'[^\n]+', text[lo:hi])) if topic=='ticket_price' and '\n' in text[lo:hi] else []
            table = next((m for m in spans if re.search(r'\|.*(?:票种|票型).*\|.*(?:价格|票价)',m[0])),None)
            rows = [(lo+m.start(),lo+m.end()) for m in spans if not re.fullmatch(r'[\s|:\-]+',m[0])
                    and m is not table]
            # A base admission statement followed by paid extras is two scopes.
            scoped_rows = []
            for start,end in rows or [(lo,hi)]:
                free = re.match(r'(?:免费(?:[（(][^）)\n]+[）)])?|[^。；，\n]{0,40}(?:大门票|普通门票|景区)[^。；，\n]{0,8}免费)[。；，;]',text[start:end]) if topic=='ticket_price' else None
                if topic=='reservation_rules':
                    free = re.match(r'[^。；\n]{0,60}(?:无需预约|免预约)[。；]',text[start:end])
                if free and free.end()<end-start:
                    scoped_rows.extend([(start,start+free.end()),(start+free.end(),end)])
                else:
                    scoped_rows.append((start,end))
            priority_context = bool(re.search(r'优(?:惠|待)政策',block['context']))
            for start,end in scoped_rows:
                raw = text[start:end].strip()
                start += len(text[start:end])-len(text[start:end].lstrip())
                end = start+len(raw)
                if INSTRUCTION.search(raw):
                    continue
                label = clean(raw).lstrip('-+| ').split('：')[0].split(':')[0]
                if re.match(r'优(?:惠|待)政策',label):
                    priority_context = True
                bundle = bool(re.search(r'套餐|含讲解|费用说明|备注|家庭票|联票|观光车|VIP|演出|体验|讲解|滑索|探险|游船|租船|营地', raw))
                mixed = bool(re.search(r'街区.*演出|普通票.*套餐', raw))
                visitor = ('adult' if re.match(r'(?:普通)?成人票',label) else 'child' if re.match(r'儿童|学生',label)
                           else 'priority' if priority_context or re.search(r'老人|免票|\d+周岁|现役|退休|离休|残疾|低保|军人|献血|皈依|凭.{0,12}证|持.{0,12}证',label)
                           else 'general' if not re.search(r'人群|老人|儿童|学生',label) else 'unknown')
                if topic != 'ticket_price':
                    visitor = 'general'
                ordinary = visitor in {'adult','general'} and not bundle
                price_basis = ('indirect' if bundle or re.search(r'未.{0,8}(?:列|提供|公布)|未直接',raw) else
                               'direct' if re.search(r'\d+(?:\.\d+)?\s*元|免费',raw) else 'not_provided')
                if table and price_basis=='not_provided':
                    header_cells=table[0].strip().strip('|').split('|')
                    cells=raw.strip().strip('|').split('|')
                    column=next((i for i,c in enumerate(header_cells) if '价' in c),-1)
                    if 0<=column<len(cells) and re.fullmatch(r'\s*[￥¥]?\s*\d+(?:\.\d+)?\s*',cells[column]):
                        price_basis='direct'
                check = date_check(raw,block['context'],dates.get(target.get('day_index',1),''))
                fact = {'schema_version':'attraction-facts-v3','target_id':binding['target_id'],'entity_poi_id':None,
                    'topic':topic,'value_raw':raw,'source_id':source['id'],
                    'statement_kind':'advice' if topic=='advice' else 'reference' if topic in {'phone','duration'} else 'rule',
                    'scope':{'visit_date':dates.get(target.get('day_index',1)) or None,'visit_mode':target.get('visit_mode','entry'),
                        'visitor_type':visitor,'product':'general' if ordinary else 'special',
                        'product_kind':'mixed' if mixed else 'bundle' if bundle else 'admission',
                        'product_label':raw if mixed or bundle else '',
                        'time_role':'opening' if topic=='opening_hours' else None},
                    'evidence':[{'kind':'provider_response_span','quote':raw,'start':start,'end':end}],
                    'verification':{'identity':'provider','entity_match':'research_target_bound','content_support':'exact_provider_span','date_applicability':'unknown'},
                    'status':'provider_answer_reference','extraction_status':'unresolved_product' if mixed else 'candidate',
                    'temporal_rule':temporal_rule(raw,dates.get(target.get('day_index',1))), 'date_check':check,
                    'rule_scope':{'bucket':'provider_answer','date_relevance':check['state']},
                    'provider_name':part['name'],'binding_basis':part['basis'],'context_raw':block['context'],
                    'context_evidence':{'kind':'provider_response_span','quote':text[block['context_start']:block['context_end']],
                                        'start':block['context_start'],'end':block['context_end']},
                    'price_basis':price_basis if topic=='ticket_price' else None, 'statements':split_statements(raw)}
                if topic=='phone':
                    purpose = re.search(r'票务咨询|联系电话|咨询电话|票务电话|总机电话',block['context'])
                    fact['contact_purpose'] = purpose[0] if purpose else '咨询电话'
                if table:
                    fact['table_header']=table[0]
                    fact['table_evidence']={'quote':table[0],'start':lo+table.start(),'end':lo+table.end()}
                if topic=='opening_hours':
                    observations=[]
                    for match in re.finditer(r'\d{1,2}[:：]\d{2}\s*[-—~～至]\s*\d{1,2}[:：]\d{2}',raw):
                        observations.append({'role':'opening_window','quote':match[0],'start':start+match.start(),'end':start+match.end()})
                    for role,words in [('last_entry','停止入园|停止入馆|停止入场|最晚入园|最晚入场|停止检票'),
                                       ('last_ticket_sale','停止售票'),('closing','清园|闭园|闭馆(?:时间)?')]:
                        for match in re.finditer(r'\d{1,2}[:：]\d{2}\s*(?:'+words+r')|(?:'+words+r')[：:\s]*\d{1,2}[:：]\d{2}',raw):
                            observations.append({'role':role,'quote':match[0],'start':start+match.start(),'end':start+match.end()})
                    fact['time_observations']=observations
                if topic=='ticket_price':
                    fact['product_candidates']=[{'raw':m[0],'start':start+m.start(),'end':start+m.end(),
                        'amounts':re.findall(r'(\d+(?:\.\d+)?)\s*元',m[0]),
                        'pricing_unit':next(iter(re.findall(r'元\s*[/／]\s*(人|张|份|船|套)',m[0])),None)}
                        for m in re.finditer(r'[^；;。]+[；;。]?',raw)]
                facts.append(fact)
    for f in facts:
        for span in [*f['evidence'],f['context_evidence']]:
            if text[span['start']:span['end']] != span['quote']:
                raise ValueError('provider_span_mismatch')
    return facts, retained


def _legacy_extract(source, targets, dates, *, extraction_version):
    if extraction_version != LEGACY_EXTRACTION_VERSION:
        raise ValueError('Legacy extraction requires an explicit supported version.')
    text = source['text']
    headings = list(re.finditer(r'^#{2,4}\s+<?([^\n<>]+)>?\s*$', text, re.M))
    facts, materials, mappings, retained = [], [], [], []
    for index, target in enumerate(targets):
        target_id = f'target-{index + 1}'
        matching = [(i, h) for i, h in enumerate(headings) if h[1].strip(' <>*') == target['name']]
        retained.append({'target_id':target_id, 'name':target['name'], 'source_id':source['id'],
                         'status':'bound' if len(matching)==1 else 'ambiguous' if matching else 'unmatched',
                         'sections':[{'name':target['name'], 'start':h.end(),
                                      'end':headings[i+1].start() if i+1<len(headings) else len(text),
                                      'heading_start':h.start(), 'basis':'exact_name'} for i,h in matching]})
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
            topic = LEGACY_LABELS.get(label)
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
            'answer_sections': retained, 'extraction_version': extraction_version,
            'unparsed_answer_retained': True}


def _section_materials(source, target, binding, blocks):
    text = source['text']
    part = binding['sections'][0]
    finish = min((block['context_start'] for block in blocks), default=part['end'])
    intro = []
    candidates = 0
    ranges = [(part['start'],finish)]+[(b['start'],b['end']) for b in blocks if b['topic']=='intro']
    if section_role(part,sections(text,include_fields=True))=='transport':
        ranges = []  # Legacy saved targets may still contain a train; its route is not cultural prose.
    for lo,hi in ranges:
        units = []
        for match in re.finditer(r'[^\n]+', text[lo:hi]):
            line = match[0]; raw = line.strip()
            if not raw:
                continue
            marker = LIST_ITEM.match(line)
            start = lo + match.start() + (marker.end() if marker else len(line)-len(line.lstrip()))
            end = lo + match.end() - (len(line)-len(line.rstrip()))
            if not marker and units and units[-1][2] and line[:1].isspace() and not raw.startswith(('#','[','|','>','```')):
                units[-1][1] = end
            else:
                units.append([start,end,bool(marker)])
        for lo_unit,hi_unit,_ in units:
            raw = text[lo_unit:hi_unit]
            if raw.startswith(('#','[','|','>','```')):
                candidates += 1
                continue
            for piece in re.finditer(r'[^。！？]+[。！？]?', raw):
                value = piece[0].strip()
                if len(value)>=8:candidates += 1
                if 8 <= len(value) <= 1000 and not INSTRUCTION.search(value) and not practical_detail(value):
                    start = lo_unit + piece.start() + len(piece[0])-len(piece[0].lstrip())
                    intro.append({'text':value, 'source_ids':[source['id']], 'start':start, 'end':start+len(value)})
    tips = [{'text':text[b['start']:b['end']], 'source_ids':[source['id']], 'kind':'advice',
             'start':b['start'], 'end':b['end']} for b in blocks if b['topic']=='advice'
            and b['end']-b['start']<=1800 and not INSTRUCTION.search(text[b['start']:b['end']])]
    material = {'target_id':binding['target_id'], 'name':target['name'], 'intro':intro, 'tips':tips,
                'source_ids':[source['id']], 'entity_poi_id':None,
                'answer_state':'reference' if intro else 'raw_pending' if not text[part['start']:part['end']].strip()
                    or ranges and (candidates or any(b['topic'] in {'intro','unknown'} for b in blocks)) else 'not_provided',
                'candidate_count':candidates, 'unparsed_count':max(0,candidates-len(intro))}
    mappings = []
    for link in re.findall(r'https://[^\s<>\)]+', text[part['start']:part['end']]):
        parsed = urlsplit(link)
        if parsed.hostname and (parsed.hostname=='ctrip.com' or parsed.hostname.endswith('.ctrip.com')):
            params = parse_qs(parsed.query)
            if params.get('poiId'):
                mappings.append({'target_id':binding['target_id'], 'ctrip_poi_id':params['poiId'][0],
                                 'poi_type':params.get('poiType',[None])[0], 'amap_poi_id':None,
                                 'basis':'provider_section_link', 'status':'unbound'})
    return material, mappings


def extract_candidates(source, targets, dates, *, city='', aliases=None):
    version = source.get('extraction_version') or EXTRACTION_VERSION
    if version == LEGACY_EXTRACTION_VERSION:
        return _legacy_extract(source, targets, dates, extraction_version=version)
    if version != EXTRACTION_VERSION:
        raise ValueError('Unsupported provider extraction version: ' + str(version))
    bindings = bind_sections(source, targets, city, aliases)
    facts, retained = extract_blocks(source, targets, dates, bindings)
    sections_by_target = {row['target_id']:row for row in retained}
    materials, mappings = [], []
    for target, binding in zip(targets, bindings):
        if binding['status'] != 'bound':
            continue
        material, links = _section_materials(source, target, binding,
                                            sections_by_target[binding['target_id']]['fields'])
        materials.append(material)
        mappings.extend(links)
    return {'facts':facts, 'activity_materials':materials, 'provider_mappings':mappings,
            'answer_sections':retained, 'unparsed_answer_retained':True, 'extraction_version':version,
            'extractor_revision':EXTRACTOR_REVISION}


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


def research_dates(request, event, duration_options=(), *, planning_spec=None):
    now = event.occurred_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING)
    arrival = parse_reminder_time(request.get('arrival_date_text', ''), now)
    visit = parse_reminder_time(request.get('visit_start_date_text', ''), now, day=arrival.day)
    # An arrival date alone is not a sightseeing start date.
    arrival_day = arrival.day if request.get('arrival_date_text') else ''
    visit_day = visit.day if request.get('visit_start_date_text') else ''
    basis, tentative = 'server_calendar_from_user_text', False
    planning_spec = planning_spec or {}
    if not visit_day and not arrival_day and planning_spec.get('start_date'):
        visit_day = planning_spec['start_date']
        tentative = bool(planning_spec.get('date_is_tentative'))
        basis = 'saved_trip_system_reference' if tentative else 'saved_trip_user_date'
    if arrival.error or visit.error:
        raise ValueError('研究日期尚不明确或已过去，请确认到达日期和开始游览日期（跨年请写年份）。')
    if arrival_day and visit_day and visit_day < arrival_day:
        raise ValueError('开始游览日期早于到达日期，请确认这两个日期。')
    days = duration_options[0] if len(duration_options) == 1 else None
    return {'timezone':'Asia/Shanghai', 'reference_date':now.astimezone(BEIJING).date().isoformat(),
            'arrival_date':arrival_day, 'visit_start_date':visit_day,
            'visit_end_date':(datetime.fromisoformat(visit_day) + timedelta(days=days-1)).date().isoformat()
                if visit_day and days else '',
            'basis':basis, 'date_is_tentative':tentative, 'year_inferred':bool(arrival_day or visit_day)
                and not any(str(year) in event.content for year in range(now.year, now.year+3))}


def day_sections(content):
    # Keep offsets stable while excluding quoted/code examples from day evidence.
    blank = lambda m: re.sub(r'[^\n]', ' ', m[0])
    masked = re.sub(r'```.*?```|~~~.*?~~~',blank,content,flags=re.S)
    masked = re.sub(r'(?m)^[ \t]*>.*$',blank,masked)
    day_pattern = rf'(?:第\s*({NUMBER})\s*天|Day\s*(\d+))(?=$|[\s：:、，,（(*—-]|[^\x00-\x7f])'
    headers = list(re.finditer(r'(?m)^[ \t]{0,3}(#{1,6})[ \t]+([^\n]*)',masked))
    sections = []
    for i,header in enumerate(headers):
        day = re.match(day_pattern,header[2].lstrip('*_ '),re.I)
        if day:
            end = next((h.start() for h in headers[i+1:] if len(h[1])<=len(header[1])),len(content))
            sections.append((number(day[1] or day[2]),header.start(),end))
    if not sections:
        # Plain-text plans may put sequential days on one line, separated by punctuation.
        markers = list(re.finditer(rf'(?m)(?:^[ \t]*(?:[-*+] |\d+[.)、] )?(?:\*\*)?|[；;，,][ \t]*){day_pattern}',masked,re.I))
        for i,marker in enumerate(markers):
            end = markers[i+1].start() if i+1<len(markers) else len(content)
            end = min([end,*[h.start() for h in headers if marker.end()<h.start()<end and len(h[1])<=2]])
            sections.append((number(marker[1] or marker[2]),marker.start(),end))
    return masked, sections


def ground_day(name, quote, content, masked, sections, maximum):
    positions = [m.start() for m in re.finditer(re.escape(name),masked)]
    quotes = [(m.start(),m.end()) for m in re.finditer(re.escape(quote),content)]
    quoted_positions = [p for p in positions if any(start<=p and p+len(name)<=end for start,end in quotes)]
    def days_at(points):
        return {day for day,start,end in sections if 1<=day<=maximum and any(start<=p<end for p in points)}
    candidates = days_at(quoted_positions) or days_at(positions)
    # Model day hints never resolve ambiguous or absent itinerary evidence.
    return next(iter(candidates)) if len(candidates)==1 else None



def source_request_text(message, request=None):
    previous = (request or {}).get('source_request', '')
    return message if not previous or previous in message else previous + '；' + message


def source_selection(text, *, automatic=False, catalog=None):
    policy = provider_policy(text, automatic=automatic)
    overrides = {}
    if re.search(r'只看|仅看|只用|仅用|只参考', text):
        urls = re.findall(r'https://[^\s<>，。]+', text)
        if urls:
            overrides.update(strict_sources=True, requested_domains=[urlsplit(url).hostname for url in urls])
        elif re.search(r'只(?:看|用|参考)高德|仅(?:看|用)高德', text):
            overrides.update(strict_sources=True, requested_domains=['amap.com'], amap_only=True)
        elif re.search(r'只看.{0,20}官网|仅看.{0,20}官网|只参考.{0,20}官方', text):
            overrides['official_only'] = True
    if catalog is not None:
        requested, excluded = [], []
        for domain, aliases in catalog['aliases'].items():
            hits = [alias for alias in [domain, *aliases] if alias.lower() in text.lower()]
            if hits:
                denied = any(re.search(rf'(?:不要|排除|不看).{{0,8}}{re.escape(alias)}|{re.escape(alias)}.{{0,8}}(?:不要|排除|不看)', text, re.I) for alias in hits)
                (excluded if denied else requested).append(domain)
        if requested or excluded:
            overrides.setdefault('requested_domains', requested)
            overrides['excluded_domains'] = excluded
            overrides.setdefault('strict_sources', bool(re.search(r'只看|仅看|仅参考|只参考|只用|仅用', text)))
    return {**policy, 'scope_overrides': overrides}


def bind_source_selection(selection, scope):
    policy = {key:value for key,value in selection.items() if key!='scope_overrides'}
    scope = {**scope, **selection.get('scope_overrides', {})}
    if scope.get('strict_sources') and not policy['explicit_requested']:
        policy.update(requested=False, only=False)
    scope.update(wendao_only=policy['only'], offline=policy['offline'],
                 official_only=policy['official_only'] or scope.get('official_only', False))
    return policy, scope


def statement_kind(text):
    if re.search(r'必须|不得|不能|停止入|须.{0,6}预约|需.{0,6}预约|闭馆|开放时间|营业时间|票价', text):
        return 'rule'
    if re.search(r'建议|最好|适合|推荐|可以|可选', text):
        return 'advice'
    if re.match(r'\s*(?:\*\*)?\d{1,2}[:：]\d{2}\s*(?:出发|午餐|晚餐)', text):
        return 'selected_action'
    return 'reference'


def split_statements(text):
    """Keep the suggestion and a genuine late-entry restriction independently."""
    return [{'text': m[0], 'kind': statement_kind(m[0]), 'start': m.start(), 'end': m.end()}
            for m in re.finditer(r'[^，,；;。]+[，,；;。]?', text)]

