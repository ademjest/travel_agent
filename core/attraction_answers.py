"""One evidence-backed answer view for chat, saved reports and planning."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import re

from core.research_contract import TOPICS
from core.wendao_answer import extract_candidates, INSTRUCTION
from core.wendao_blocks import clean
from core.rule_semantics import resolve_rules

VERSION = 'attraction-answer-v1'
STATES = {'reference':'已有参考', 'limited':'有限参考', 'conflict':'来源分歧',
          'raw_pending':'原文待整理', 'not_provided':'未提供', 'not_queried':'未查询'}


def has_provider(data):
    return any(s.get('provider')=='ctrip_wendao' for s in data.get('sources', []))


def target_dates(data, targets):
    values = data.get('dates') or data.get('provider_dates') or {}
    start = values.get('visit_start_date') or values.get('start_date')
    result = {}
    for target in targets:
        day = target.get('day_index')
        if start and type(day) is int and day>=1:
            result[day]=(date.fromisoformat(start)+timedelta(days=day-1)).isoformat()
    if not start:
        for index,target in enumerate(targets):
            day = target.get('day_index')
            scoped = {f['scope']['visit_date'] for f in data.get('facts',[]) if f['target_id']==f'target-{index+1}'
                      and f.get('scope',{}).get('visit_date')}
            if type(day) is int and len(scoped)==1:
                result[day]=next(iter(scoped))
    return result


def working_copy(data):
    """Replay saved responses in memory, never mutate a historical run or its reply."""
    value = deepcopy(data)
    targets = value.get('target_scope', {}).get('targets', value.get('provider_targets', []))
    city = value.get('target_scope', {}).get('city') or value.get('destination', '')
    if not city:
        cities = {r.get('detail', {}).get('city', '').removesuffix('市') for r in value.get('place_details', []) if r.get('detail')}
        city = next(iter(cities)) if len(cities)==1 else ''
    for source in value.get('sources', []):
        if source.get('provider')!='ctrip_wendao' or not source.get('text'):
            continue
        parsed = extract_candidates(source, targets, target_dates(value, targets), city=city)
        value['facts']=[f for f in value.get('facts', []) if f['source_id']!=source['id']]+parsed['facts']
        value['answer_sections']=parsed['answer_sections']
        value['activity_materials']=parsed['activity_materials']
    resolve_rules(value.get('facts',[]))
    return value


def fact_key(fact):
    return hashlib.sha256(json.dumps([fact['source_id'],fact['target_id'],fact['topic'],fact['value_raw'],
        [(e.get('start'),e.get('end')) for e in fact.get('evidence',[])]],ensure_ascii=False).encode()).hexdigest()[:16]


def supported(fact, sources, section=None):
    source = sources.get(fact.get('source_id'))
    if not source or INSTRUCTION.search(fact.get('value_raw','')):
        return False
    if source.get('provider')=='ctrip_wendao':
        if not section or section.get('status')!='bound':
            return False
        boundary = section['sections'][0]
        spans = fact.get('evidence', [])
        valid = bool(spans) and all(e.get('kind')=='provider_response_span'
            and boundary['start']<=e.get('start',-1)<e.get('end',-1)<=boundary['end']
            and source['text'][e['start']:e['end']]==e.get('quote')==fact['value_raw'] for e in spans)
        context = fact.get('context_evidence')
        if context:
            valid = valid and (boundary['start']<=context.get('start',-1)<=context.get('end',-1)<=boundary['end']
                and source['text'][context['start']:context['end']]==context.get('quote'))
        table = fact.get('table_evidence')
        if table:
            valid = valid and (boundary['start']<=table.get('start',-1)<table.get('end',-1)<=boundary['end']
                and source['text'][table['start']:table['end']]==table.get('quote'))
        return valid
    if source.get('provider')=='amap':
        if fact.get('status')!='provider_reference':
            return False
        for span in fact.get('evidence',[]):
            if span.get('kind')!='api_field' or span.get('path') not in {'business.opentime_today','business.opentime_week'}:
                return False
            key = span['path'].split('.')[1]
            if source.get('snapshot',{}).get('business',{}).get(key)!=span.get('raw_value') or span.get('raw_value')!=fact['value_raw']:
                return False
        return bool(fact.get('evidence'))
    if fact.get('status') in {'official_verified','conflict'} and fact.get('verification',{}).get('identity')=='official':
        return (source.get('official_identity',{}).get('status')=='verified' and
            any(e.get('kind')=='web_quote' and source.get('text','')[e.get('start',0):e.get('end',0)]==e.get('quote')
                for e in fact.get('evidence',[])))
    return False


def ordinary(fact):
    scope = fact['scope']
    return (scope.get('visitor_type') in {'adult','general','personal','unknown'}
            and scope.get('product','general')=='general'
            and fact.get('extraction_status')!='unresolved_product')


def display(raw):
    return re.sub(r'\s+', ' ', clean(raw).strip('-+| ')).strip()


def opening_signature(raw):
    match = re.search(r'(\d{1,2})[:：](\d{2})\s*[-—~～至]\s*(\d{1,2})[:：](\d{2})', raw)
    return tuple(map(int,match.groups())) if match else None


def api_text(raw):
    # Keep normal hours and their own conditions; obsolete dated branches stay in source detail.
    raw = re.split(r'\s+(?=\d{1,2}月\d{1,2}日)|[,，](?=20\d{2}年春节)|[；;]',raw)[0]
    raw = re.sub(r'[（(]数据来源[^）)]*[）)]', '',raw)
    return display(raw)


def select_slot(tid, topic, facts, sources, section, retrieval, visit_date=None):
    rows = [f for f in facts if f['target_id']==tid and f['topic']==topic and supported(f,sources,section)]
    official = [f for f in rows if f['status']=='official_verified' and ordinary(f)
                and (not visit_date or f['scope'].get('visit_date')==visit_date)
                and f.get('rule_resolution') not in {'superseded','outside','conflict'}
                and f.get('rule_scope',{}).get('bucket','current')=='current']
    official_conflicts = [f for f in rows if f.get('verification',{}).get('identity')=='official' and ordinary(f)
                          and (f['status']=='conflict' or f.get('rule_resolution')=='conflict')
                          and (not visit_date or f['scope'].get('visit_date')==visit_date)]
    provider = [f for f in rows if f['status']=='provider_answer_reference' and f.get('rule_resolution')!='superseded']
    amap = [f for f in rows if f['status']=='provider_reference'
            and f['verification'].get('date_applicability') not in {'not_target_day','expired','outside_declared_window'}]
    amap.sort(key=lambda f: any(e.get('path')=='business.opentime_week' for e in f.get('evidence',[])), reverse=True)
    selected, notes, state, text = [], [], 'not_provided', '来源未提供该项信息。'
    raw_evidence = None
    if retrieval=='not_queried':
        text, state = '本次未查询该景点。', 'not_queried'
    elif retrieval=='requested_unreturned':
        text = '已请求该景点，但问道本次未返回对应章节。'
    if official_conflicts:
        selected, state = official_conflicts, 'conflict'
        text = '同范围官方资料存在冲突，暂不能确定：'+'；'.join(display(f['value_raw']) for f in official_conflicts)
    elif official:
        selected, text, state = [official[0]], display(official[0]['value_raw']), 'reference'
        selected += [f for f in provider if display(f['value_raw'])==text and ordinary(f)]
    elif provider:
        if topic=='ticket_price':
            direct = [f for f in provider if ordinary(f) and f.get('price_basis')=='direct'
                      and f.get('date_check',{}).get('state')!='outside']
            direct.sort(key=lambda f:f['scope'].get('visitor_type')!='adult')
            if direct:
                selected, text, state = [direct[0]], display(direct[0]['value_raw']), 'reference'
                if direct[0].get('table_header'):
                    text = '表头：'+display(direct[0]['table_header'])+'；'+text
                if direct[0].get('date_check',{}).get('state')=='target_unknown':
                    text = '来源报价适用'+direct[0]['date_check']['declared']+'：'+text+'；目标日期尚未确定，需重新核对。'
                    state='limited'
            else:
                selected, state = provider[:1], 'limited'
                indirect = [f for f in provider if f.get('price_basis')=='indirect']
                outside = [f for f in provider if ordinary(f) and f.get('price_basis')=='direct' and f.get('date_check',{}).get('state')=='outside']
                text = '问道未直接提供普通成人票金额。'
                if outside:
                    selected=[outside[0]]
                    text = '来源报价适用'+outside[0]['date_check'].get('declared','其他日期')+'：'+display(outside[0]['value_raw'])+'；本次目标日期不同，需重新核对。'
                elif indirect:
                    selected = [indirect[0]]
                    text += '套餐/产品备注线索：'+display(indirect[0]['value_raw'])
                else:
                    text += '已列优惠或免票人群条件，见详情。'
        elif topic=='opening_hours':
            candidates = [f for f in provider if ordinary(f)] or provider
            valid = [f for f in candidates if f.get('date_check',{}).get('state')!='outside'
                     and f.get('rule_resolution')!='outside']
            if valid:
                selected, text, state = [valid[0]], display(valid[0]['value_raw']), 'reference'
                if not opening_signature(text) and not re.search(r'全天|24小时',text):
                    state = 'limited'
                if amap and opening_signature(text) and opening_signature(amap[0]['value_raw']) and opening_signature(text)!=opening_signature(amap[0]['value_raw']):
                    state = 'conflict'
                    selected.append(amap[0])
                    notes.append('高德常规参考：'+api_text(amap[0]['value_raw'])+'；开门时间存在差异，需确认。')
            else:
                selected, state = candidates[:1], 'limited'
                invalid = candidates[0]
                declared = invalid.get('date_check',{}).get('declared','原文声明区间')
                text = '目标日开放时间需确认。问道声明'+declared+'，却将目标日期归入该区间；该日期适用判断不成立。'
                if amap:
                    selected.append(amap[0])
                    notes.append('高德常规参考：'+api_text(amap[0]['value_raw'])+'；不足以确认目标日。')
        else:
            general = [f for f in provider if ordinary(f)]
            chosen = (general or provider)[0]
            selected, text, state = [chosen], display(chosen['value_raw']), 'reference' if general else 'limited'
            if not general:
                notes.append('仅为所述产品/服务的条件，不能作为普通入园的通用规则。')
            if re.search(r'每笔|份|\d+天内|提前\d+天|预订',text):
                notes.append('以上为来源产品/渠道条件，适用范围待确认。')
    elif amap and topic=='opening_hours':
        selected, text, state = [amap[0]], api_text(amap[0]['value_raw']), 'limited'
        notes.append('高德营业参考，目标日待确认。')
    elif section and section.get('status')=='bound':
        source = sources[section['source_id']]
        part = section['sections'][0]
        from core.wendao_blocks import field_blocks
        blocks = [b for b in field_blocks(source['text'], part) if b['topic']==topic]
        words = {'opening_hours':r'开放|营业|开门|闭馆', 'ticket_price':r'(?:\d+(?:\.\d+)?\s*元|免费)', 'reservation_rules':r'预约|购票|预订'}[topic]
        for line in re.finditer(r'[^\n]+',source['text'][part['start']:part['end']]):
            position = part['start']+line.start()
            if (re.search(words,line[0]) and not INSTRUCTION.search(line[0])
                    and (topic!='ticket_price' or any(b['start']<=position<b['end'] for b in blocks)
                         or bool(re.search(r'(?:普通|成人|儿童|学生)票\s*\d+(?:\.\d+)?\s*元',line[0])))):
                state, text = 'raw_pending', '尚未结构化整理：'+line[0].strip()
                raw_evidence = {'source_id':source['id'],'quote':line[0],
                                'start':part['start']+line.start(),'end':part['start']+line.end()}
                break
    if section and section.get('status')=='ambiguous' and not selected:
        state, text = 'raw_pending', '多个章节竞争同一对象，名称对应关系待确认；相关原文见来源详情。'
    verification = 'official_verified' if any(f['status']=='official_verified' for f in selected) else 'provider_reference' if selected else 'unknown'
    return {'target_id':tid,'topic':topic,'text':text,'notes':notes,'answer_state':state,
            'verification_state':verification,'source_ids':list(dict.fromkeys([*[f['source_id'] for f in selected],
                 *([raw_evidence['source_id']] if raw_evidence else [])])), 'raw_evidence':raw_evidence,
            'fact_ids':[fact_key(f) for f in selected], 'facts':selected,
            'retrieval_state':retrieval,'selection_reason':'applicable_official_then_provider_then_map',
            'alternatives':[fact_key(f) for f in rows if f not in selected]}


def answer_view(data):
    value = working_copy(data)
    targets = value.get('target_scope',{}).get('targets',value.get('provider_targets',[]))
    topics = value.get('target_scope',{}).get('fields',{}).get('topics',list(TOPICS))
    sources = {s['id']:s for s in value.get('sources',[])}
    sections = {s['target_id']:s for s in value.get('answer_sections',[])}
    requested = value.get('provider_requested_target_ids') or value.get('requested_target_ids')
    if requested is None:
        summary = value.get('wendao_call',{}).get('query_summary','')
        if '本批仅查询：' in summary:
            requested = [f'target-{i+1}' for i,t in enumerate(targets) if t['name']+'（' in summary.split('\n')[0]]
        else:
            requested = list(sections)
    entries = []
    for i,target in enumerate(targets):
        tid = f'target-{i+1}'
        section = sections.get(tid)
        retrieval = 'returned' if section and section['status']=='bound' else 'requested_unreturned' if tid in requested else 'not_queried'
        visit_date = target_dates(value,targets).get(target.get('day_index'))
        slots = [select_slot(tid,t,value.get('facts',[]),sources,section,retrieval,visit_date) for t in topics]
        extras = [f for f in value.get('facts',[]) if f['target_id']==tid and f['topic'] in {'phone','duration'}
                  and supported(f,sources,section)]
        extras = list({(f.get('contact_purpose',f['topic']),f['value_raw']):f for f in extras}.values())
        chosen = {key for s in slots for key in s['fact_ids']}
        variants = [f for f in value.get('facts',[]) if f['target_id']==tid and f['topic'] in topics
                    and fact_key(f) not in chosen and supported(f,sources,section)]
        material = next((m for m in value.get('activity_materials',[]) if m['target_id']==tid), {})
        entries.append({'target_id':tid,'name':target['name'],'visit_date':target_dates(value,targets).get(target.get('day_index')),
                        'material':material,
                        'retrieval_state':retrieval,'slots':slots,'extras':extras,'section':section,'variants':variants})
    slots = [s for e in entries for s in e['slots']]
    evidence_scope = hashlib.sha256(json.dumps([[s.get('content_hash'),s.get('retrieved_at'),s.get('text','')]
        for s in sources.values() if s.get('provider')=='ctrip_wendao'],ensure_ascii=False).encode()).hexdigest()[:12]
    for slot in slots:
        slot['source_anchors']={str(sid):f"source-{evidence_scope}-{sid}-{slot['target_id']}-{slot['topic']}" for sid in slot['source_ids']}
    counts = {state:sum(s['answer_state']==state for s in slots) for state in STATES}
    official = sum(s['verification_state']=='official_verified' and s['answer_state']=='reference' for s in slots)
    dates = value.get('dates') or value.get('provider_dates') or {}
    used = {key for s in slots for key in s['source_ids']}
    shown_sources = [{k:s[k] for k in ('id','type','provider','title','url','text','retrieved_at','content_hash','snapshot','official_identity') if k in s}
                     for s in sources.values() if s['id'] in used or s.get('provider')=='ctrip_wendao']
    return {'schema_version':VERSION,'evidence_scope':evidence_scope,'entries':entries,'sources':shown_sources,'dates':dates,
            'metrics':{'target_count':len(targets),'collected_target_count':sum(e['retrieval_state']=='returned' for e in entries),
                       'slot_count':len(slots),'usable_slot_count':counts['reference'],'official_verified_count':official,
                       'states':counts,'parse_omission_count':counts['raw_pending']},
            'answer_stage':'collected' if all(e['retrieval_state']=='returned' for e in entries) else 'partial',
            'diagnostics':{'application_request_counts':value.get('request_counts',{}),
                           'provider_call':{k:v for k,v in value.get('wendao_call',{}).items() if k in
                                            {'physical_calls','status','elapsed_ms','bytes','cost','usage'}},
                           'note':'应用请求计数不等于所有供应商底层物理请求或账单。'},
            'source_retrieved_at':next((s.get('retrieved_at') for s in sources.values() if s.get('provider')=='ctrip_wendao'),None),
            'projected_at':value.get('updated_at') or next((s.get('retrieved_at') for s in sources.values() if s.get('retrieved_at')), '')}


def slot_markdown(slot):
    refs = ''.join(f"[来源{sid}](#{slot['source_anchors'][str(sid)]})" for sid in slot['source_ids'])
    badge='〔官方依据已核验〕' if slot['verification_state']=='official_verified' and slot['answer_state']=='reference' else ''
    return '- '+TOPICS[slot['topic']]+'：'+slot['text']+badge+(' '+refs if refs else '')+(' '+' '.join(slot['notes']) if slot['notes'] else '')


def render_answer(view, *, detailed=False, max_chars=6000):
    dates = view['dates']
    start = dates.get('visit_start_date') or dates.get('start_date')
    end = dates.get('visit_end_date') or dates.get('end_date')
    header = ['## 景点旅行信息', '以下主要依据携程问道整理，尚未独立核实；已有适用官方证据按项标注。']
    if start:
        header.append(('出发日尚未确定，日期暂定：' if dates.get('date_is_tentative') else '本次参考日期：')
                      +start+('至'+end if end and end!=start else '')+'；改期后需重新核对。')
    footer = ['\n完整条件、票种、来源片段和未结构化原文可在研究报告查看；查看不会重新查询。']
    # Equal budgets for targets; field conditions and dissent travel as one indivisible unit.
    allowance = max(0,(max_chars-len('\n'.join(header+footer))-80)//max(1,len(view['entries'])))
    sections = []
    for entry in view['entries']:
        lines = ['\n### '+entry['name']]
        if not detailed and all(s['answer_state']=='not_queried' for s in entry['slots']):
            sections += lines+['- 本批未查询；可另行明确继续查询。']
            continue
        for slot in entry['slots']:
            line = slot_markdown(slot)
            if not detailed and len('\n'.join(lines))+len(line)>allowance:
                line = '- '+TOPICS[slot['topic']]+'：完整条件较长，已保存在详情，不能据节选确定。'
            lines.append(line)
        for fact in entry['extras']:
            label = fact.get('contact_purpose','咨询电话')+'（问道提供）' if fact['topic']=='phone' else '推荐游玩时长（参考）'
            line = '- '+label+'：'+display(fact['value_raw'])
            if detailed or len('\n'.join(lines))+len(line)<=allowance:
                lines.append(line)
        if detailed and entry['variants']:
            lines+=['\n#### 票种优惠与其他范围']+['- '+display(f['value_raw']) for f in entry['variants']]
        if not detailed and len('\n'.join(lines))>allowance:
            lines=[lines[0],'- '+'／'.join(TOPICS[s['topic']] for s in entry['slots'])+'：完整条件见详情，不能据节选确定。']
        sections+=lines
    if detailed:
        sections.append('\n## 来源与原文')
        for source in view['sources']:
            if source.get('text'):
                sections += ['\n### 来源'+str(source['id']),source.get('title','来源')+'；原查询时间：'+str(source.get('retrieved_at','未知')),source['text']]
        sections += ['\n## 诊断',json.dumps({'metrics':view['metrics'],**view['diagnostics']},ensure_ascii=False)]
    return '\n'.join(header+sections+footer)
