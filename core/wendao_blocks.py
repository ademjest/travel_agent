"""Bounded Markdown sections and exact spans; entity binding does not certify policy."""
from datetime import date
import json
from pathlib import Path
import re

from core.rule_semantics import temporal_rule, split_statements

LABELS = {'开放时间':'opening_hours', '营业时间':'opening_hours', '开园时间':'opening_hours',
          '普通票价':'ticket_price', '门票价格':'ticket_price', '门票':'ticket_price', '票价':'ticket_price',
          '预约要求':'reservation_rules', '预约方式':'reservation_rules', '预订要求':'reservation_rules',
          '预约规则':'reservation_rules', '票务咨询':'phone', '联系电话':'phone', '咨询电话':'phone', '票务电话':'phone', '总机电话':'phone',
          '推荐游玩时长':'duration', '游玩时长':'duration', '玩法建议':'advice', '游玩建议':'advice'}
LABEL = '|'.join(sorted(LABELS, key=len, reverse=True))
FIELD = re.compile(r'^[ \t]*(?:[-+*]\s+|\d+[.)、]\s+)?(?:\*\*)?(?P<label>'+LABEL+
                   r')(?P<context>[（(][^）)\n]*[）)])?(?:\*\*)?\s*[：:]\s*(?:\*\*)?')
HEAD = re.compile(r'^(?:#{1,6}[ \t]+(?P<md>[^\n]+)|\*\*(?P<bold>[^*\n]+)\*\*[ \t]*)$', re.M)
BOUNDARY = re.compile(r'入口|出口|停车场|售票处|游客中心|[东西南北]门$')


def clean(text):
    return re.sub(r'[<>*`]', '', text).strip()


def sections(text):
    title = lambda m: clean(m.group('md') or m.group('bold'))
    heads = [m for m in HEAD.finditer(text) if not re.fullmatch('(?:'+LABEL+r')(?:[（(][^）)]*[）)])?[：:]?', title(m))]
    return [{'name':title(m), 'start':m.end(), 'end':heads[i+1].start() if i+1<len(heads) else len(text),
             'heading_start':m.start()} for i,m in enumerate(heads)]


def bind_sections(source, targets, city='', aliases=None):
    text = source['text']
    city = city.removesuffix('市')
    catalog = json.loads((Path(__file__).resolve().parents[1]/'data/attraction_aliases.json').read_text(encoding='utf-8'))
    catalog += aliases or []
    result = []
    for index,target in enumerate(targets):
        name, tid = target['name'], f'target-{index+1}'
        candidates = []
        for section in sections(text):
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
        result.append({'target_id':tid, 'name':name, 'source_id':source['id'],
                       'status':'bound' if len(candidates)==1 else 'ambiguous' if candidates else 'unmatched',
                       'sections':candidates})
    # One returned chapter cannot silently identify two competing research targets.
    counts = {}
    for row in result:
        for part in row['sections']:
            counts[part['start']] = counts.get(part['start'], 0)+1
    for row in result:
        if row['status']=='bound' and counts[row['sections'][0]['start']]>1:
            row['status']='ambiguous'
    return result


def field_blocks(text, section):
    start, end = section['start'], section['end']
    lines = list(re.finditer(r'[^\n]+(?:\n|$)', text[start:end]))
    starts = []
    for i,line in enumerate(lines):
        raw = line[0].rstrip('\r\n')
        match = FIELD.match(raw)
        if match:
            starts.append((i, LABELS[match['label']], match.end(), match.group('context') or ''))
        elif (heading := re.fullmatch(r'('+LABEL+r')([（(][^）)\n]*[）)])?', clean(raw).lstrip('#').strip())):
            starts.append((i, LABELS[heading[1]], len(raw), heading[2] or ''))
        elif raw.lstrip().startswith('|'):
            cells = [clean(c) for c in raw.strip().strip('|').split('|')]
            if cells and cells[0] in LABELS:
                payload = raw.find('|', raw.find('|')+1)+1
                starts.append((i, LABELS[cells[0]], payload, ''))
            elif len(cells)>1 and any('价格' in c or '票价' in c for c in cells) and any('票种' in c for c in cells):
                if not starts or starts[-1][1]!='ticket_price':
                    starts.append((i, 'ticket_price', len(raw), raw))
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
        if body_start>=finish:
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
    try:
        target = date.fromisoformat(visit_date or '')
    except ValueError:
        return result
    if declared:
        result.update(state='matching' if declared[1]==target.isoformat() else 'outside', declared=declared[1])
    window = re.search(r'(?:每年)?(\d{1,2})月(\d{1,2})日\s*[-—~～至]\s*(?:(次年|来年))?(\d{1,2})月(\d{1,2})日', raw)
    if window:
        a, b = (int(window[1]),int(window[2])), (int(window[4]),int(window[5]))
        md = (target.month,target.day)
        inside = a<=md<=b if a<=b else md>=a or md<=b
        result.update(state='outside' if not inside or result['state']=='outside' else 'matching',
                      declared=result.get('declared', window[0]))
        if not inside and re.search(r'处于此时段|正常开放|属于.{0,6}时段', raw):
            result['issue']='target_outside_declared_window'
    elif '全年' in raw and result['state']=='unknown':
        result['state']='regular'
    temporary = temporal_rule(raw,visit_date)
    if temporary['explicit_override'] and temporary['applicability']=='matching' and not (declared and result['state']=='outside'):
        result.update(state='matching',temporary_override=True)
        result.pop('issue',None)
    return result


def extract_blocks(source, targets, dates, bindings):
    from core.wendao_answer import INSTRUCTION
    facts, retained = [], []
    text = source['text']
    for i,(target,binding) in enumerate(zip(targets,bindings)):
        if binding['status']!='bound':
            retained.append(binding)
            continue
        part = binding['sections'][0]
        blocks = field_blocks(text,part)
        retained.append({**binding,'fields':blocks})
        for block in blocks:
            topic, lo, hi = block['topic'], block['start'], block['end']
            spans = list(re.finditer(r'[^\n]+', text[lo:hi])) if topic=='ticket_price' and '\n' in text[lo:hi] else []
            table = next((m for m in spans if re.search(r'\|.*(?:票种|票型).*\|.*(?:价格|票价)',m[0])),None)
            rows = [(lo+m.start(),lo+m.end()) for m in spans if not re.fullmatch(r'[\s|:\-]+',m[0])
                    and m is not table]
            # A base admission statement followed by paid extras is two scopes.
            scoped_rows = []
            for start,end in rows or [(lo,hi)]:
                free = re.match(r'[^。；，\n]{0,40}(?:大门票|普通门票|景区)[^。；，\n]{0,8}免费[。；，]',text[start:end]) if topic=='ticket_price' else None
                if topic=='reservation_rules':
                    free = re.match(r'[^。；\n]{0,60}(?:无需预约|免预约)[。；]',text[start:end])
                if free and free.end()<end-start:
                    scoped_rows.extend([(start,start+free.end()),(start+free.end(),end)])
                else:
                    scoped_rows.append((start,end))
            priority_context = False
            for start,end in scoped_rows:
                raw = text[start:end].strip()
                start += len(text[start:end])-len(text[start:end].lstrip())
                end = start+len(raw)
                if INSTRUCTION.search(raw):
                    continue
                label = clean(raw).lstrip('-+| ').split('：')[0].split(':')[0]
                if re.fullmatch(r'[\s*：:]*优(?:惠|待)政策[^\n]*',raw):
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
