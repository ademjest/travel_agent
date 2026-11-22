from infrastructure.skill_manager import skill_text, skill_asset, skill_request
from tools.research.evidence import planning_source
"""Research workflow owner; source/evidence helpers never perform its I/O or writes."""
from tools.trip.tool import PlanningArtifacts
from tools.trip.constraints import plan_destination, plan_durations
import json
import re
import time
import logging
from dataclasses import dataclass
from tools.research.sources import (
    VERSION as PROVIDER_FACT_VERSION,
    EXTRACTION_VERSION,
    EXTRACTOR_REVISION,
    research_dates,
    reference_dates,
    answer_targets,
    extract_candidates,
    provider_policy,
    source_selection,
    source_request_text,
    bind_source_selection,
    day_sections,
    ground_day,
)
from tools.research.evidence import (
    RESEARCH_EVIDENCE_VERSION,
    FACT_SCHEMA_VERSION,
    WRITTEN_ANSWER_VERSION,
    fingerprint,
    limits,
    presentation,
    render,
    writing_messages,
    validate_written,
    validate_research_items,
    ResearchEvidenceError,
    EXCERPT_CHARS,
    SOURCE_PREFIX,
    attraction_report,
    has_provider,
    bind_poi,
    page_facts,
    provider_facts,
    reconcile_facts,
    validate_api_fact,
    poi_detail_view,
    resolve_rules,
    evidence_view,
)
from datetime import date, datetime, timedelta, timezone
from copy import deepcopy
from urllib.parse import urlsplit, urldefrag
from core.tasks import TaskUpdate
from core.commands import location_answer
from core.execution_scope import ensure_execution_active, CURRENT_EXECUTION, ExecutionRevoked
from infrastructure.public_http import PublicHTTPError
from infrastructure.web_reader import read_page, public_text_issue
from core.model_budget import model_call_context
from core.context_contract import complete_json, response_json
from core.research_contract import ResearchRequestError, SCHEMA_VERSION, MAX_ITEMS, TARGET_TEXT_CHARS, MAX_TEXT_CHARS, TOPICS, project_request, infer_plan_reference
from infrastructure.wendao_client import WendaoError
from core.data_paths import PROJECT_ROOT
from core.time_input import NUMBER
from services.official_sources import public_policy_url, scoped_url
from core.model_policy import output_limit_for_purpose, model_for_purpose


logger = logging.getLogger(__name__)
SKILL_DIR = PROJECT_ROOT / 'skills' / 'travel-research'


class ResearchSearchPlanner:
    def __init__(self, client=None, model='', skill_dir=SKILL_DIR):
        self.client, self.model, self.skill_dir = client, model, skill_dir

    @property
    def skill_manager(self):
        from infrastructure.skill_manager import SkillManager, get_skill_manager
        return get_skill_manager() if self.skill_dir == SKILL_DIR else SkillManager(research_dir=self.skill_dir)

    _day_sections = staticmethod(day_sections)

    _ground_day = staticmethod(ground_day)

    @skill_request
    def plan_targets(self, request, plan):
        """One bounded semantic extraction; only quoted public entities reach search."""
        if self.client is None:
            raise ValueError('未配置研究模型，无法可靠提取原方案景点；请配置模型或直接提供景点清单。')
        content = plan['content']
        if len(content) > 18000:
            raise ValueError('该方案过长，无法完整核对所有景点，请先指定需要研究的天数或景点。')
        skill = skill_text('research.targets', 'policy')
        catalog = skill_asset('research.targets').resource('sources')
        catalog['aliases']['amap.com'] = ['高德', 'amap']
        with model_call_context('research_targets', prompt_version='research-targets-v3', stage_id='research.targets'):
            response = self.client.chat.completions.create(model=self.model,response_format={'type':'json_object'},messages=[
                {'role':'system','content':((skill + skill_text('research.targets', 'system')) + json.dumps(catalog,ensure_ascii=False))},
                {'role':'user','content':complete_json({'request':request['original_request'],
                    'validated_fields':{k:request[k] for k in ('topics','arrival_date_text','visit_start_date_text') if k in request},
                    'destination':plan['destination'],'untrusted_plan':content},24000)}])
        value = response_json(response)
        raw = value.get('targets')
        if not isinstance(raw,list) or not 1 <= len(raw) <= 40:
            raise ValueError('原方案景点清单未能可靠提取，请明确需要核验的景点。')
        targets, seen = [], set()
        options = plan.get('duration_options',[])
        masked, sections = self._day_sections(content)
        corrected_count, unresolved_count = 0, 0
        for row in raw:
            if not isinstance(row,dict):
                raise ValueError('景点清单结构无效，尚未搜索。')
            name, quote, hint = row.get('name'),row.get('source_text'),row.get('day_index')
            if (not isinstance(name,str) or not 1 <= len(name) <= 80
                    or not isinstance(quote,str) or not 1 <= len(quote) <= 500 or quote not in content or name not in quote
                    or type(row.get('optional')) is not bool or row.get('visit_mode') not in {'entry','exterior'}):
                raise ValueError('景点或参观方式无法对应原方案，尚未搜索。')
            day = self._ground_day(name,quote,content,masked,sections,max(options or [14]))
            key = (name,day,row['visit_mode'])
            if key not in seen:
                corrected = day is not None and hint is not None and (type(hint) is not int or hint!=day)
                corrected_count += int(corrected)
                unresolved_count += int(day is None)
                targets.append({**{k:row[k] for k in ('name','source_text','optional','visit_mode')},
                                'day_index':day,'day_resolution':'unresolved' if day is None else
                                    'corrected' if corrected else 'source_grounded'})
                seen.add(key)
        # An entrance ticket is not implied by a plan that only visits the exterior.
        for target in targets:
            clauses = [c for c in re.split(r'[。；;\n]',content) if target['name'] in c]
            if clauses and all(re.search(r'外观|外部参观|远眺|exterior',c,re.I) for c in clauses):
                target['visit_mode'] = 'exterior'
            if clauses and all(re.search(r'可选|备选|可去|时间充裕|替换|若',c) for c in clauses):
                target['optional'] = True
        fields, ignored_dates = {}, []
        for key in ('arrival_date_text', 'visit_start_date_text'):
            if key in request:
                fields[key] = request[key]
                continue
            extracted = value.get(key, '')
            if extracted and isinstance(extracted, str) and extracted in request['original_request']:
                fields[key] = extracted
            elif extracted:
                ignored_dates.append(key)
        extracted_topics = value.get('topics',[])
        if not isinstance(extracted_topics,list):
            raise ValueError('研究主题结构无效，尚未搜索。')
        fields.update(query_text=request['query_text'],topics=[*request.get('topics',[]),*extracted_topics])
        fields, _ = project_request(fields,request['original_request'])
        if not fields.get('topics'):
            raise ValueError('请说明需要核验票价、开放时间还是预约规则；尚未搜索。')
        requested, excluded = value.get('requested_domains',[]),value.get('excluded_domains',[])
        for domains in (requested,excluded):
            if (not isinstance(domains,list) or len(domains)>6 or any(not isinstance(d,str) or d not in catalog['aliases']
                    or not any(alias.lower() in request['original_request'].lower() for alias in [d,*catalog['aliases'][d]]) for d in domains)):
                raise ValueError('研究来源限制无法对应本轮要求，尚未搜索。')
        strict, language = value.get('strict_sources',False),value.get('language','zh-cn')
        if (type(strict) is not bool or (strict and not requested) or set(requested)&set(excluded)
                or not isinstance(language,str) or not re.fullmatch(r'[a-z]{2,3}(?:-[a-z]{2})?',language)):
            raise ValueError('研究来源或语言设置无效，尚未搜索。')
        return {'targets':targets,'fields':fields,'requested_domains':requested,'excluded_domains':excluded,
                'strict_sources':strict,'language':language,
                'ignored_date_fields': ignored_dates,
                'day_corrected_count':corrected_count,'day_unresolved_count':unresolved_count}

    @skill_request
    def plan(self, text):
        fallback = {'region':'unknown','batches':[{'query':text,'domains':[]}],
                    'excluded_domains':[], 'strict_sources':False,
                    'destination':'', 'requested_domains':[],
                    'note':'未能可靠判断目的地，使用通用公开检索。'}
        try:
            skill = skill_text('research.search_plan', 'policy')
            catalog = skill_asset('research.search_plan').resource('sources')
            source_scope = source_selection(text, catalog=catalog)['scope_overrides']
            requested, excluded = source_scope.get('requested_domains', []), source_scope.get('excluded_domains', [])
            mentioned=bool(requested or excluded)
            destination_match=re.search(rf'(?:想去|搜索|查(?:一下)?|了解|规划|安排)([\u4e00-\u9fff]{{2,10}}?)(?=玩|(?:{NUMBER})[天日](?:游)?|攻略|旅行|旅游|行程)',text)
            safe_destination=destination_match[1] if destination_match else ''
            if mentioned:
                strict=bool(re.search(r'只看|仅看|仅参考|只参考|只用|仅用',text))
                domains=[d for d in requested if d not in excluded]
                fallback = {**fallback,'destination':safe_destination,'requested_domains':domains,
                    'excluded_domains':excluded,'strict_sources':strict,
                    'batches':([{'query':text,'domains':domains,'language':'zh-cn'}] if domains else []),
                    'note':('仅检索你指定的平台；来源不足时不跨平台补充。' if strict else
                            '优先你指定的平台；目的地区域未可靠分类，必要时使用通用公开检索。')}
                if not strict and domains:
                    fallback['batches'].append({'query':text,'domains':[],'language':'zh-cn'})
            if self.client is None:
                return fallback
            with model_call_context('research_plan', prompt_version='research-search-plan-v1', stage_id='research.search_plan'):
                response = self.client.chat.completions.create(model=self.model,response_format={'type':'json_object'},messages=[
                    {'role':'system','content':((skill + skill_text('research.search_plan', 'system')) + json.dumps(catalog,ensure_ascii=False))},
                    {'role':'user','content':complete_json({'request':text},12000)}])
            value = response_json(response)
            if not isinstance(value,dict) or value.get('region') not in {'china','international','mixed','unknown'}:
                raise ValueError('invalid_region')
            destination=value.get('destination','')
            if not isinstance(destination,str) or not destination or len(destination)>80 or destination not in text:
                raise ValueError('invalid_destination')
            for key in ('query','official_query'):
                if not isinstance(value.get(key),str) or not value[key].strip() or len(value[key])>300:
                    raise ValueError('invalid_query')
            language = value.get('language','zh-cn' if value['region']=='china' else 'en')
            if not isinstance(language,str) or not re.fullmatch(r'[a-z]{2,3}(?:-[a-z]{2})?',language):
                raise ValueError('invalid_language')
            requested, excluded = value.get('requested_domains',[]), value.get('excluded_domains',[])
            for domains in (requested,excluded):
                if not isinstance(domains,list) or len(domains)>6:
                    raise ValueError('invalid_domains')
                for domain in domains:
                    if not isinstance(domain,str) or domain not in catalog['aliases']:
                        raise ValueError('unknown_domain')
                    if not any(alias.lower() in text.lower() for alias in [domain,*catalog['aliases'][domain]]):
                        raise ValueError('ungrounded_domain')
            strict = value.get('strict_sources',False)
            reference = value.get('platform_request','')
            if (type(strict) is not bool or not isinstance(reference,str) or len(reference)>300
                    or ((requested or excluded or strict) and (not reference or reference not in text))
                    or (strict and not requested) or set(requested)&set(excluded)):
                raise ValueError('invalid_platform_preference')
            groups = catalog['groups']
            batches = []
            def add(query, domains):
                allowed = [d for d in domains if d not in excluded]
                if domains and not allowed:
                    return
                batches.append({'query':query,'domains':allowed,'language':language})
            if requested:
                add(value['query'],requested)
            if not strict:
                if value['region']=='china':
                    for group in ('china_community','china_travel'):
                        if requested and group=='china_community':
                            continue
                        add(value['query'],groups[group])
                elif value['region']=='international':
                    add(value['query'],groups['international_travel'])
                elif value['region']=='mixed':
                    add(value['query'],groups['china_travel'])
                    add(value['query'],groups['international_travel'])
                else:
                    add(value['query'],[])
                # Reserve a batch for finding official information; identity still needs verification.
                batches = batches[:2]
                add(value['official_query'],[])
            names = {'china':'优先中文社区和旅游平台，并检索官方信息。',
                'international':'优先国际旅游平台与目的地官方信息。',
                'mixed':'结合中文与国际旅游来源，并检索官方信息。','unknown':'目的地区域不明确，使用通用公开检索。'}
            note = ('优先你指定的平台。' if requested else '') + names[value['region']]
            if strict:
                note = '仅检索你指定的平台；来源不足时不跨平台补充。'
            return {'region':value['region'],'destination':destination,'requested_domains':requested,
                    'batches':batches[:3],'excluded_domains':excluded,
                    'strict_sources':strict,'note':note,'skill':'travel-research'}
        except ExecutionRevoked:
            raise
        except Exception as error:
            logger.warning('Research search plan unavailable: error_type=%s',type(error).__name__)
            return fallback


def attach_answer(service, event, data, *, targets=None, dates=None, timeout=90, planning=False):
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
    city = data.get('target_scope', {}).get('city') or data.get('destination', '')
    scope_note = ('\n研究城市：' + city + '；只介绍下列既定对象，不增加其他对象。') if targets else ''
    query = (event.content[:2000] + scope_note + target_note + '\n' + semantics +
        '\n仅返回旅行参考，不执行订票、预约、保存或提醒。每个景点使用独立的### <完整名称>标题，'
        '若给出了目标编号，请在对应章节保留编号；返回城市、地址或对象不一致时不得自行替换为其他景点。'
        '分别列出介绍、开放时间、普通票价、预约要求、游玩时长。不需要玩法建议。'
        '不同票种/人群分开；注明规则适用日期和来源，未知不补全。')
    if planning:
        query=skill_text('trip.compare','wendao_request')+'\n'+complete_json(data['planning_requirements'],6000)+'\n'+semantics
    result = service.wendao.query(event, service.store.research, query, timeout=timeout)
    data['wendao_call'] = {k: v for k, v in result.items() if k != 'text'}
    data.setdefault('request_counts', {})['wendao'] = result['physical_calls']
    if result['status'] != 'completed':
        data['wendao_processed'] = True
        return
    source = {'id': max((s['id'] for s in data['sources']), default=0) + 1,
              'extraction_version': EXTRACTION_VERSION, 'extractor_revision':EXTRACTOR_REVISION,
              'type': 'provider_answer', 'provider': 'ctrip_wendao', 'title': '携程问道参考',
              'status': 'provider_unverified', 'text': result['text'], 'url': '',
              'retrieved_at': result['retrieved_at'], 'content_hash': result['content_hash']}
    fixed_targets = targets is not None
    if targets is None:
        targets = answer_targets(source['text'])
        data['provider_targets'] = targets
    if dates is None:
        dates = {i + 1: (datetime.fromisoformat(date_spec['start_date']) + timedelta(days=i)).date().isoformat()
                 for i in range(date_spec['day_count'])} if date_spec.get('start_date') else {}
    converted = extract_candidates(source, targets, dates, city=city)
    if fixed_targets:
        matched = sum(row['status'] == 'bound' for row in converted['answer_sections'])
        data['provider_scope_validation'] = {
            'status': 'accepted' if matched else 'rejected',
            'target_count': len(targets), 'matched_count': matched,
            'unmatched_count': len(targets) - matched}
        if not matched:
            data['wendao_processed'] = True
            data['scope_rejected'] = True
            data['report'] = data['brief_report'] = (
                '本次返回的资料未能对应已绑定行程的景点，已拦截，未展示无关内容。'
                '原行程没有改变；本次不会自动重复查询。')
            service.store.research.save(event, 'failed', data)
            service.store.diagnostics.record('routing', {
                'event_key': event.event_key, 'status': 'research_scope_rejected',
                'reason': 'provider_scope_mismatch',
                'target_count': len(targets), 'matched_count': 0,
                'unmatched_count': len(targets)})
            raise WendaoError('scope_mismatch')
    data['sources'].append(source)
    data.setdefault('facts', []).extend(converted.pop('facts'))
    data.update(converted, fact_schema=PROVIDER_FACT_VERSION, wendao_processed=True)
    data['provider_coverage'] = {'candidate_count': len(data['facts']), 'target_count': len(targets),
                                 'official_verified_count': 0, 'hourly_validation': 'not_performed'}
    resolve_rules(data['facts'])
    service.store.research.save(event, 'reading', data)


def answer_report(data, *, detailed=False):
    if isinstance(data, dict) and data.get('scope_rejected'):
        return data['report']
    from tools.research.evidence import evidence_view
    selected = evidence_view(data)
    data = selected.data
    if any(s.get('provider')=='ctrip_wendao' for s in data.get('sources', [])):
        return render(presentation(selected), detailed=detailed)
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
    from tools.research.evidence import has_provider, evidence_view
    selected = evidence_view(data)
    if has_provider(selected.data):
        view=selected.answer
        data.update(answer_schema=view['schema_version'],answer_metrics=view['metrics'],answer_stage=view['answer_stage'],
                    report=answer_report(selected,detailed=True),brief_report=answer_report(selected))
    else:
        data.update(report=answer_report(selected,detailed=True),brief_report=answer_report(selected))
    return selected


def remaining_seconds(store):
    ensure_execution_active()
    scope = CURRENT_EXECUTION.get()
    if scope is None:
        return 100.0
    with store._connect() as db:
        row = db.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?',(scope.job_id,)).fetchone()
    return (datetime.fromisoformat(row['deadline_at'])-datetime.now(timezone.utc)).total_seconds()


@skill_request
def write_answer(service, event, data, *, evidence=None, available=None):
    if data.get('scope_rejected'):
        return data['report']
    base = presentation(evidence if evidence is not None else data)
    if (base['metrics'].get('invalid_section_count') and base['metrics']['invalid_section_count']==len(base['entries'])):
        data.update(report=render(base,detailed=True),brief_report=render(base),answer_schema=WRITTEN_ANSWER_VERSION)
        service.store.research.save(event,'partial',data)
        service.store.diagnostics.record('routing',{'event_key':event.event_key,'status':'research_parse_incomplete',
            'reason':'empty_provider_sections','target_count':len(base['entries'])})
        return data['brief_report']
    asset = skill_asset('research.write')
    metadata = {'schema_version':WRITTEN_ANSWER_VERSION, 'prompt_version':asset.version, 'model':model_for_purpose(service.model,'travel_answer_write'),
                'source_hash':fingerprint([(s.get('content_hash'),s.get('text')) for s in data.get('sources',[])]),
                'snapshot':{k:data.get(k) for k in ('dates','provider_dates','target_scope','plan_title','plan_version','plan_snapshot')},
                'request':event.content, 'started_at':datetime.now(timezone.utc).isoformat()}
    metadata.update(asset.metadata())
    record, acquired = service.store.research.reserve_answer_write(event, metadata)
    if not acquired:
        if record.get('result'):
            data['answer_write'] = record
            return render(presentation(data))
        # Reserved/unknown calls never get a second submission after recovery.
        return render(base)
    data['answer_write'] = record
    started, submitted, received = time.monotonic(), False, False
    try:
        _, timeout_limit = limits()
        timeout = min(timeout_limit, remaining_seconds(service.store)-10)
        if service.client is None:
            raise ValueError('model_unconfigured')
        if timeout < 5:
            raise ValueError('insufficient_time')
        messages, supplied = writing_messages(base, event.content, metadata['snapshot'])
        from infrastructure.model_gateway import estimate_input
        record['input_estimate'] = estimate_input(messages)
        with model_call_context('travel_answer_write', prompt_version=asset.version, context_policy_version=asset.version, stage_id='research.write'):
            submitted = True
            response = service.client.chat.completions.create(model=service.model, response_format={'type':'json_object'},
                messages=messages, max_tokens=output_limit_for_purpose('travel_answer_write',8192), timeout=timeout)
        received = True
        usage = getattr(response,'usage',None)
        record['usage'] = {key:getattr(usage,key,None) for key in ('prompt_tokens','completion_tokens','total_tokens')}
        result = validate_written(response_json(response), base, supplied)
        record['status'] = 'completed'
    except ExecutionRevoked:
        raise
    except Exception as error:
        result = base
        from core.model_budget import ModelBudgetExceeded
        record.update(status='unknown' if submitted and not received and not isinstance(error,ModelBudgetExceeded) else 'fallback', error_type=type(error).__name__)
        result['degradations'].append(str(error) if isinstance(error,ValueError) and not submitted else type(error).__name__)
        service.store.diagnostics.record('routing', {'event_key':event.event_key,
            'status':'research_answer_fallback', 'purpose':'travel_answer_write',
            'answer_status':record['status'], 'reason':result['degradations'][-1], 'error_type':type(error).__name__})
    ensure_execution_active()
    record.update(elapsed_ms=round((time.monotonic()-started)*1000),
                  finished_at=datetime.now(timezone.utc).isoformat())
    result['diagnostics']['writing'] = {key:record.get(key) for key in ('status','model','input_estimate','usage','elapsed_ms','error_type')}
    result['diagnostics']['writing'].update(generation=result['generation'],degradations=result['degradations'])
    record['result'] = result
    data.update(answer_write=record, brief_report=render(result), report=render(result,detailed=True), answer_schema=WRITTEN_ANSWER_VERSION)
    service.store.research.save(event, data.get('stage','partial'), data)
    return data['brief_report']


def plan_identity(plan):
    snapshot = plan.get('itinerary_snapshot') or {}
    return (snapshot.get('trip_id') or plan.get('artifact_id') or plan.get('title'), plan.get('version'))


def saved_trip_plan(trip):
    """Project an owned saved trip, preserving its exact committed version."""
    from tools.trip.tool import TripTool
    return {
        'content': TripTool.format_trip(trip), 'title': trip['title'],
        'destination': trip['spec']['destination'],
        'duration_options': [trip['spec']['day_count']], 'version': trip['version'],
        'planning_spec': deepcopy(trip['spec']),
        'itinerary_snapshot': {
            'trip_id': trip['trip_id'], 'version': trip['version'],
            'days': [
                {'day_index': day['day_index'], 'date': day.get('date'),
                 'activities': [
                     {'poi_id': activity['poi_id'],
                      'name': trip['sources'][activity['poi_id']]['name'],
                       'period': activity['period'],
                       'subactivities':[{'poi_id':child['poi_id'],'name':trip['sources'][child['poi_id']]['name']}
                           for child in activity.get('subactivities',[])]}
                     for activity in day['activities']]}
                for day in trip['plan']['days']]}}


def structured_target_scope(request, plan):
    """Use committed POIs for full-trip research; no model re-extraction."""
    snapshot = plan.get('itinerary_snapshot')
    if not snapshot:
        return None
    # Only the whole-sentence bare follow-up gets this fast path. Named subsets,
    # dates and source constraints retain the existing semantic extraction path.
    if not infer_plan_reference(request['original_request']) or request.get('source_request'):
        return None
    targets, seen = [], set()
    for day in snapshot['days']:
        for parent in day['activities']:
            for activity in [parent,*parent.get('subactivities',[])]:
                key = (activity['poi_id'], day['day_index'])
                if key in seen:
                    continue
                seen.add(key)
                targets.append({'name': activity['name'], 'poi_id': activity['poi_id'],
                    'visit_group_id':parent['poi_id'],
                    'source_text': activity['name'], 'day_index': day['day_index'],
                    'optional': False, 'visit_mode': 'entry',
                    'day_resolution': 'saved_trip_snapshot'})
    if not targets or len(targets) > 40:
        raise ValueError('已保存行程的景点范围为空或超过研究上限，尚未查询。')
    return {'targets': targets,
            'fields': {**{key: request[key] for key in
                ('query_text', 'arrival_date_text', 'visit_start_date_text') if key in request},
                'topics': request.get('topics') or list(TOPICS)},
            'requested_domains': [], 'excluded_domains': [],
            'strict_sources': False, 'language': 'zh-cn'}


def resolve_plan(store, event, request):
    """Explicit historical references win; generic follow-ups use saved trips."""
    artifacts = list(store.planning.list_for_owner(event))
    trips = list(store.trips.list_for_owner(event))

    def with_dates(artifact):
        spec = store.trips.planning_dates_for_event(event, artifact.get('source_event_id', ''))
        return {**artifact, 'planning_spec': spec} if spec is not None else artifact

    reference = request.get('plan_reference', '')
    bare_reference = infer_plan_reference(event.content)
    generic = bool(bare_reference or infer_plan_reference(reference))
    historical = bool(re.search(r'最初|最早|最开始|原(?:来)?(?:的)?(?:方案|行程)|旧(?:方案|行程)', reference))
    # plan_destination is a heuristic: don't run it against a bare pointer.
    destination = '' if bare_reference else plan_destination(event.content, '')
    if reference and re.search(r'(?:这个|该|当前|这份|原来的)$', destination):
        destination = ''
    if destination:
        artifacts = [a for a in artifacts if a['destination'] == destination]
        trips = [t for t in trips if t['spec'].get('destination') == destination]
    index = request.get('plan_index')
    if index is not None and (not generic or historical):
        indexed = [a for a in artifacts if a['ordinal'] == index]
        if not indexed:
            return None, 'unavailable_reference'
        return with_dates(indexed[0]), 'explicit_reference'
    titled = [a for a in artifacts if a['title'] in event.content]
    if len(titled) == 1:
        return with_dates(titled[0]), 'explicit_title'
    trip_titles = [t for t in trips if t['title'] in event.content]
    if len(trip_titles) == 1:
        return saved_trip_plan(trip_titles[0]), 'explicit_trip_title'
    if historical:
        return ((with_dates(artifacts[0]), 'historical_plan') if len(artifacts) == 1
                else (None, 'ambiguous_plan' if artifacts else 'missing_plan'))
    if event.reply_to_id:
        linked_trip = store.trips.referenced(event)
        if linked_trip and any(t['trip_id'] == linked_trip['trip_id'] for t in trips):
            return saved_trip_plan(linked_trip), 'reply_saved_trip'
        with store._connect() as db:
            link = db.execute('''SELECT event_id FROM outbound_message_links
                WHERE platform=? AND scope_id=? AND message_id=? AND owner_id=?''',
                (event.platform, event.storage_scope_id, event.reply_to_id, event.sender_id)).fetchone()
        linked = [a for a in artifacts if a['source_event_id'] in
                  {event.reply_to_id, link['event_id'] if link else ''}]
        if len(linked) == 1:
            return with_dates(linked[0]), 'reply_link'
    if trips:
        return ((saved_trip_plan(trips[0]), 'saved_trip') if len(trips) == 1
                else (None, 'ambiguous_plan'))
    with store._connect() as db:
        last = db.execute('''SELECT user_msg_id FROM conversation_turns
            WHERE group_openid=? AND member_openid=? ORDER BY id DESC LIMIT 1''',
            (event.storage_scope_id, event.sender_id)).fetchone()
    recent = [a for a in artifacts if last and a['source_event_id'] == last['user_msg_id']]
    if len(recent) == 1:
        return with_dates(recent[0]), 'adjacent_plan'
    if len(artifacts) == 1:
        return with_dates(artifacts[0]), 'unique_plan'
    return None, 'ambiguous_plan' if artifacts else 'missing_plan'

class AttractionResearch:
    def __init__(self, service, amap, registry):
        self.service, self.amap, self.registry = service, amap, registry

    def _allowed(self, url, scope):
        if scope.get('wendao_only') or scope.get('offline'):
            return False
        allowed = public_policy_url(url) and self.service._domain_allowed(url, scope['requested_domains'] if scope['strict_sources'] else [], scope['excluded_domains'])
        if scope.get('official_only'):
            allowed = allowed and any(scoped_url(url, entry) for entry in self.registry.entries)
        return allowed

    def _save(self, event, data, stage='reading'):
        ensure_execution_active()
        self.service.store.research.save(event, stage, data)

    @staticmethod
    def _source(data, **values):
        source = {'id': max((row['id'] for row in data['sources']), default=0) + 1, **values}
        data['sources'].append(source)
        return source

    def run(self, event, request, data, available, *, selection=None):
        service = self.service
        scope = {**data['target_scope'], 'city': request['plan']['destination']}
        data['target_scope']['city'] = scope['city']
        if selection is None:
            wendao = service.wendao
            selection = source_selection(source_request_text(event.content, request),
                automatic=wendao is not None and wendao.configured)
        policy, scope = bind_source_selection(selection, scope)
        data['target_scope'] = scope
        data['provider_policy'] = policy
        data.setdefault('fact_schema', FACT_SCHEMA_VERSION)
        data.setdefault('facts', [])
        data.setdefault('place_details', [])
        data.setdefault('target_progress', {})
        data.setdefault('request_counts', {'amap': 0, 'pages': 0, 'search': 0})
        topics = scope['fields']['topics']
        verification_requested = bool(scope.get('official_only') or re.search(r'核实|核验|核对|确认.{0,8}(?:官网|规定|能否入园|抢票)|官方.*(?:确认|规定)', event.content))
        data['research_mode'] = 'verification' if verification_requested else 'information'
        if policy['requested'] and available():
            try:
                if service.wendao is None:
                    raise WendaoError('not_configured')
                dates = {t.get('day_index', 1): service._target_date(t, data['dates']) for t in scope['targets']}
                timeout = min(75, getattr(available, 'remaining_seconds', lambda: 115)() - getattr(available,'writing_reserve',40))
                attach_answer(service, event, data, targets=scope['targets'], dates=dates, timeout=timeout)
            except WendaoError as error:
                data['wendao_error'] = str(error)
                if error.code == 'scope_mismatch':
                    return data['report']
        from tools.research.evidence import answer_view, has_provider
        initial_view = answer_view(data) if has_provider(data) else None
        # Navigation has an explicit place before optional fact enrichment.
        if initial_view:
            service.search_tool.discover_links(event, data, available)
        gap_states = {'conflict', 'not_provided', 'limited'}
        for index, target in enumerate(scope['targets'][:4]):
            ensure_execution_active()
            if not available():
                break
            target_id = f'target-{index + 1}'
            progress = data['target_progress'].setdefault(target_id, {'page_ids': [], 'searches': []})
            if progress.get('done'):
                continue
            entry = self.registry.match(target['name'], scope['city'])
            visit_date = service._target_date(target, data['dates'])
            selected = next((e for e in initial_view['entries'] if e['target_id']==target_id), None) if initial_view else None
            needs_confirmation = verification_requested or not initial_view
            # Ordinary research uses existing references. Only a real gap or discrepancy
            # starts external verification; unavailable optional sources do not fan out.
            if selected:
                needs_confirmation = needs_confirmation or any(s['answer_state'] in gap_states for s in selected['slots'])
            needs_map = verification_requested or not initial_view or bool(selected and any(
                s['topic']=='opening_hours' and s['answer_state'] not in {'reference','raw_pending'} for s in selected['slots']))
            if 'amap_status' not in progress:
                candidates, reason = [], 'amap_not_configured'
                permitted = self._allowed('https://restapi.amap.com/v5/place/text', scope)
                permitted = permitted and not re.search(r'不(?:要|用|必).{0,6}高德', event.content)
                lookup_id = getattr(self.amap, 'get_poi_detail', None)
                lookup_text = getattr(self.amap, 'search_poi_details', None)
                supported = callable(lookup_text) or (entry and entry.get('poi_id') and callable(lookup_id))
                if self.amap is not None and permitted and supported and needs_map:
                    data['request_counts']['amap'] += 1
                    try:
                        if entry and entry.get('poi_id') and callable(lookup_id):
                            found = lookup_id(entry['poi_id'])
                            candidates = [found] if found else []
                        else:
                            candidates = list(lookup_text(scope['city'], target['name']))
                        reason = 'entity_ambiguous_or_unmatched'
                    except ExecutionRevoked:
                        raise
                    except (ValueError, OSError, RuntimeError) as error:
                        reason = getattr(error, 'code', '') or 'amap_unavailable'
                elif not permitted:
                    reason = 'user_source_restriction'
                detail = bind_poi(target, scope['city'], candidates, entry)
                progress['amap_status'] = 'matched' if detail else reason
                record = {'target_id': target_id, 'status': progress['amap_status'], 'detail': detail,
                          'entity_poi_id': detail['poi_id'] if detail else None, 'navigation_poi_id': None,
                          'candidates': [{key: row.get(key) for key in ('poi_id', 'name', 'address', 'city', 'parent_id')} for row in candidates]}
                if detail:
                    record['display'] = poi_detail_view(detail)
                data['place_details'].append(record)
                if detail:
                    source = self._source(data, type='api', provider='amap', status='provider_reference',
                        title=detail['name'], interface_path=detail['interface_path'], poi_id=detail['poi_id'],
                        retrieved_at=detail['retrieved_at'], content_hash=detail['content_hash'], snapshot=detail['snapshot'])
                    for fact in provider_facts(detail, source['id'], target_id, visit_date, target['visit_mode']):
                        validate_api_fact(fact, detail)
                        data['facts'].append(fact)
                self._save(event, data)
            urls = ([*entry['policy_urls'], *entry.get('notice_index_urls', [])][:2] if entry and needs_confirmation else [])
            # User-supplied URLs may be read, but never become registered identities.
            urls += re.findall(r'https://[^\s<>，。]+', event.content)[:2]
            if not progress.get('official_done'):
                for url in dict.fromkeys(urls):
                    if not available():
                        break
                    page_entry = entry if entry and scoped_url(url, entry) else None
                    source = service.search_tool.read_source(event, data, url, target, page_entry, scope, progress, available)
                    if source and source.get('status') == 'read':
                        for link in source.get('links', []):
                            if (entry and link['url'] != source['url'] and scoped_url(link['url'], entry)
                                    and re.search(r'临时闭馆|开放调整|预约须知|门票调整', link['text'])):
                                service.search_tool.read_source(event, data, link['url'], target, entry, scope, progress, available)
                                break
                progress['official_done'] = all(any(urldefrag(url)[0] in [urldefrag(row.get('requested_url', ''))[0],
                    urldefrag(row.get('url', ''))[0]] and row['status'] != 'loading'
                    for row in data['sources']) or not self._allowed(url, scope) for url in dict.fromkeys(urls))
                self._save(event, data)
            pages = [source for source in data['sources'] if source['id'] in progress['page_ids']]
            facts = [fact for source in pages for fact in page_facts(source, target, target_id, topics, visit_date,
                     entry if source.get('official_identity', {}).get('status') == 'verified' else None)]
            missing = [topic for topic in topics if not any(f['topic'] == topic and f['status'] == 'official_verified' for f in facts)]
            if initial_view and not verification_requested and selected:
                missing = [s['topic'] for s in selected['slots'] if s['answer_state'] in gap_states]
            if (missing and not scope.get('amap_only') and not scope.get('wendao_only') and not scope.get('offline') and service.search is not None
                    and getattr(service.search, 'configured', True) and available()):
                # Two bounded discovery attempts; model-generated titles are not identity evidence.
                while len(progress['searches']) < (2 if verification_requested or not initial_view else 1) and data['request_counts']['search'] < 8 and available():
                    query = ' '.join((scope['city'], target['name'], visit_date,
                                      ' '.join(TOPICS[t] for t in missing), '游客须知' if not progress['searches'] else '开放调整 公告'))
                    kwargs = {}
                    domains = scope['requested_domains'] if scope['strict_sources'] else entry['allowed_hosts'] if entry else []
                    if domains:
                        kwargs['include_domains'] = domains
                    if scope['excluded_domains']:
                        kwargs['exclude_domains'] = scope['excluded_domains']
                    try:
                        rows = service.search.search(service._query(query), **kwargs)
                    except (ValueError, OSError):
                        rows = []
                    progress['searches'].append({'results': rows[:2]})
                    data['request_counts']['search'] += 1
                    self._save(event, data, 'searching')
                for search in progress['searches']:
                    for row in search['results']:
                        page_entry = entry if entry and scoped_url(row['url'], entry) else None
                        source = service.search_tool.read_source(event, data, row['url'], target, page_entry, scope, progress, available)
                        if source and source['id'] not in progress['page_ids']:
                            progress['page_ids'].append(source['id'])
            pages = [source for source in data['sources'] if source['id'] in progress['page_ids']]
            data['facts'] = [fact for fact in data['facts'] if fact['target_id'] != target_id or fact['status'] in {'provider_reference', 'provider_answer_reference'}]
            data['facts'].extend(fact for source in pages for fact in page_facts(source, target, target_id, topics, visit_date,
                entry if source.get('official_identity', {}).get('status') == 'verified' else None))
            progress['done'] = available() and progress.get('official_done', False)
            self._save(event, data)
        reconcile_facts(data['facts'])
        resolve_rules(data['facts'])
        for record in data['place_details']:
            for material in data.get('activity_materials', []):
                if record['target_id'] == material['target_id']:
                    material['entity_poi_id'] = record['entity_poi_id']
            for mapping in data.get('provider_mappings', []):
                if record['target_id'] == mapping['target_id'] and record['entity_poi_id']:
                    mapping.update(amap_poi_id=record['entity_poi_id'], status='bound', basis='unique_city_name_amap_match')
        if not has_provider(data):
            service.search_tool.discover_links(event, data, available)
        selected_evidence = evidence_view(data)
        data['facts'] = selected_evidence.data['facts']
        covered = sum(any(f['target_id'] == f'target-{i + 1}' and f['topic'] == topic and f['status'] == 'official_verified'
                      and f.get('rule_scope', {}).get('bucket', 'current') == 'current'
                      and f['scope'].get('visitor_type') in {'general', 'personal'} and f['scope'].get('product', 'general') == 'general'
                      for f in data['facts']) for i in range(len(scope['targets'])) for topic in topics)
        total = len(scope['targets']) * len(topics)
        data['coverage'] = {'target_count': len(scope['targets']), 'topic_count': len(topics), 'covered_count': covered,
                            'remaining_count': total - covered, 'platform_fact_count': sum(f['status'] == 'provider_reference' for f in data['facts']),
                            'conflict_count': sum(f['status'] == 'conflict' for f in data['facts'])}
        data['coverage'].update(provider_candidate_count=sum(f['status'] == 'provider_answer_reference' for f in data['facts']),
            candidate_conflict_count=sum(f.get('rule_resolution') == 'conflict' and f['status'] == 'provider_answer_reference' for f in data['facts']),
            soft_advice_count=sum(f.get('statement_kind') == 'advice' for f in data['facts']))
        data['report'] = self.report(selected_evidence)
        data['brief_report'] = attraction_report(selected_evidence)
        if has_provider(data):
            view = selected_evidence.answer
            data['answer_schema'] = view['schema_version']
            data['answer_metrics'] = view['metrics']
            data['answer_stage'] = view['answer_stage']
        if data.get('wendao_error'):
            data['report'] += '\n' + data['wendao_error']
            data['brief_report'] += '\n' + data['wendao_error']
        self._save(event, data, 'completed' if covered == total else 'partial')
        if hasattr(service.store, 'diagnostics'):
            service.store.diagnostics.record('routing', {'event_key': event.event_key,
                'status': 'research_completed' if covered == total else 'research_partial', **data['coverage'],
                'amap_requests': data['request_counts']['amap'], 'read_pages': data['request_counts']['pages'],
                'search_requests': data['request_counts']['search']})
        if has_provider(data):
            data['stage'] = 'completed' if covered == total else 'partial'
            return write_answer(service, event, data, evidence=selected_evidence, available=available)
        return data['brief_report']

    @staticmethod
    def report(data):
        return attraction_report(data, detailed=True)


@dataclass(frozen=True)
class PlanningResearch:
    source: str | None
    note: str = ''
    provider_answer: bool = False


FACT_BATCHES = 2
FACT_BATCH_SIZE = 2
FACT_TIME_SECONDS = 150


def research_request(text, *, policy=None):
    platforms=r'小红书|知乎|马蜂窝|携程|问道|去哪儿|穷游|Tripadvisor|Lonely Planet|Wikivoyage'
    return (policy or provider_policy(text))['explicit_requested'] or bool(re.search(r'官网|官方网站|重新整理|润色.{0,8}(?:介绍|资料)|查看.{0,8}(?:研究原文|资料原文|景点依据)|联网|搜索|搜一下|搜一搜|查.{0,15}攻略|读.{0,15}https://|研究结果|研究进度|'
        rf'(?:网上|网络|{platforms}).{{0,30}}(?:攻略|游记|怎么玩)|(?:攻略|游记).{{0,30}}(?:网上|网络|{platforms})', text, re.I))


def research_continuation(text, tasks):
    pending = [task for task in tasks if task.task_type == 'research' and task.status == 'collecting']
    return pending[0] if len(pending) == 1 and location_answer(text) else None


class ResearchTool:
    def __init__(self, store, search, client=None, model='', reader=read_page, *, amap=None, official_sources=None, wendao=None,
                 official_links_enabled=True, website_search=None):
        self.store, self.search, self.client, self.model = store, search, client, model
        self.reader = reader
        self.wendao = wendao
        from tools.search import SearchTool
        self.search_tool = website_search or (SearchTool(store,search,reader=reader,registry=official_sources,enabled=official_links_enabled) if official_sources is not None else None)
        self.search_planner = ResearchSearchPlanner(client,model)
        self.attraction_research = None
        if official_sources is not None:
            self.attraction_research = AttractionResearch(self, amap, official_sources)

    @skill_request
    def prepare_for_planning(self, event, spec):
        candidate = deepcopy(spec)
        reference = self._prepare_planning(event, candidate)
        return PlanningResearch(reference, candidate.get('research_note', ''))

    def read_snapshot(self, event):
        current = self.store.research.get(event)
        if current and current.get('report'):
            return current
        return next((row for row in self.store.research.latest(event)
                     if row.get('report') and row['stage'] in {'completed', 'partial'}), None)

    def read_for_planning(self, event):
        row = self.read_snapshot(event)
        if row is None:
            return PlanningResearch(None, '当前会话没有可用于规划的研究结果，请先搜索攻略。')
        if row.get('provider_policy', {}).get('only'):
            return PlanningResearch(None, '资料限定只用问道，尚未授权地图核验，不能自动生成正式行程。')
        usable = any(s.get('text') and s.get('status') in {'read', 'snippet', 'provider_unverified'}
                     for s in row.get('sources', []))
        if not usable or has_provider(row) and not row.get('activity_materials'):
            return PlanningResearch(None, '尚未取得可用于规划的研究依据；可另行直接规划行程。')
        return PlanningResearch(self._planning_source(row), '', has_provider(row))

    def _question(self, event, claim, task):
        update = TaskUpdate('research', 'collecting', task.initial_request if task else event.content,
            {}, ('destination',), task.task_id if task else '', task.version if task else None)
        return self.store.reminders.commit(event, claim, update, '想查哪个城市或景点的攻略？请补充目的地。')

    def _prepare_planning(self, event, spec):
        """Enabled provider research for a new plan; never invokes trip operations."""
        automatic = self.wendao is not None and self.wendao.configured
        policy = provider_policy(event.content, automatic=automatic)
        if not automatic or not policy['requested']:
            return None
        data = self.store.research.get(event) or {'query': event.content[:500], 'sources': []}
        data['destination'] = spec['destination']
        data['provider_policy'] = policy
        data['provider_dates'] = {key: spec[key] for key in ('start_date', 'end_date', 'day_count',
            'date_is_tentative', 'date_reference_day', 'date_preference_text', 'defer_start_date') if key in spec}
        try:
            attach_answer(self, event, data)
        except WendaoError as error:
            spec['research_note'] = str(error)
            return None
        if self.attraction_research is not None:
            self.search_tool.discover_links(event, data, lambda: remaining_seconds(self.store) > limits()[1] + 10)
        selected_evidence = save_answer_views(data)
        self.store.research.save(event, 'partial', data)
        if data.get('wendao_call', {}).get('status') != 'completed':
            spec['research_note'] = '问道本次未取得完整回答，已保留调用状态；以下先用地点资料规划，景点规则仍待核验。'
            return None
        spec['research_note'] = ('已获取携程问道参考，完整景点资料可在研究报告查看；关键信息尚未独立核实。'
                                 if data.get('activity_materials') else
                                 '已获取问道完整回答，但尚未可靠提取景点介绍；可在研究报告查看原文。')
        return self._planning_source(data, evidence=selected_evidence)

    @skill_request
    def handle(self, event, claim, *, force=False, tasks=None):
        saved_reply = self._saved_answer_request(event)
        if saved_reply is not None:
            return saved_reply
        if tasks is None:
            tasks = self.store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id)
        task = research_continuation(event.content, tasks)
        policy = provider_policy(event.content, automatic=self.wendao is not None and self.wendao.configured)
        if not force and not research_request(event.content, policy=policy) and task is None:
            return None
        if policy['offline']:
            return '没有发起联网搜索。'
        repo = self.store.research
        if event.content.strip() in ('查看研究结果', '查看研究进度'):
            rows = repo.latest(event)
            if rows and rows[0].get('wendao_processed'):
                return answer_report(rows[0])
            return '\n\n'.join(attraction_report(row) if row.get('fact_schema') == 'attraction-facts-v1' and row.get('report')
                else row.get('report') or '研究阶段：' + row['stage'] for row in rows[:1]) or '当前会话没有研究结果。'
        if event.content.startswith('根据研究结果'):
            material = self.read_for_planning(event)
            return material.note or '已读取当前会话的研究资料。'
        text = (task.initial_request + '；目的地：' + event.content) if task else event.content
        if re.fullmatch(r'(?:帮我|请)?(?:查|查询|搜索|搜一下)(?:一份|近期|最新)?(?:旅游|旅行)?攻略[。！?？ ]*', text):
            return self._question(event, claim, task)
        data = repo.get(event) or {'query': text[:500], 'sources': []}
        if policy['requested']:
            data['provider_policy'] = policy
            try:
                if self.wendao is None:
                    raise WendaoError('not_configured')
                attach_answer(self, event, data)
            except WendaoError as error:
                return str(error)
            selected_evidence = save_answer_views(data)
            repo.save(event, 'partial', data)
            PlanningArtifacts(self.store.planning).capture(event, data['report'], kind='research_backed',
                destination=plan_destination(event.content, data['report']),
                duration_options=plan_durations(event.content, data['report']))
            return write_answer(self, event, data, evidence=selected_evidence)
        if not data.get('searched'):
            repo.save(event, 'searching', data)
            urls = re.findall(r'https://[^\s<>，。]+', text)
            try:
                results = ([{'url': url, 'title': '用户提供的公开页面', 'snippet': '', 'published_at': ''} for url in urls[:5]]
                           if urls else self._search(event,data,self._query(text)))
            except (ValueError, PublicHTTPError) as error:
                data['report'] = str(error) + '\n未生成未经搜索的攻略。'
                repo.save(event, 'failed', data)
                return data['report']
            unique = {}
            for row in results:
                url = row['url']
                parsed = urlsplit(url)
                if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
                    unique.setdefault(url.split('#')[0], row)
            data.update(results=list(unique.values())[:6], searched=True)
            repo.save(event, 'reading', data)
        if not data.get('report'):
            self._read_sources(event,data)
            plan = data.get('search_plan',{})
            if (plan and not plan.get('strict_sources')
                    and any(b['domains'] for b in plan['batches'])
                    and not any(s['status']=='read' for s in data['sources'])):
                extra = self._fallback(event,data,self._query(text))
                existing = {s['url'].split('#')[0] for s in data['results']}
                data['results'].extend(r for r in self._select([extra],2) if r['url'].split('#')[0] not in existing)
                repo.save(event,'reading',data)
                self._read_sources(event,data)
            repo.save(event, 'synthesizing', data)
            data['report_items'] = []
            data['report'] = self._summarize(text, data['sources'], self.store.preferences.defaults(event)['values'],
                                           strategy_note=data.get('search_plan',{}).get('note',''),
                                           evidence_sink=data['report_items'])
            data['report_validation'] = RESEARCH_EVIDENCE_VERSION if data['report_items'] else 'source_excerpt_only'
            repo.save(event, 'completed' if data['sources'] and all(s['status'] == 'read' for s in data['sources']) else 'partial', data)
        PlanningArtifacts(self.store.planning).capture(event,data['report'],kind='research_backed',
            destination=data.get('search_plan',{}).get('destination',''),
            duration_options=plan_durations(event.content,''))
        if task:
            self.store.tasks.prepare_result(event, claim, TaskUpdate('research', 'completed', task.initial_request,
                {}, task_id=task.task_id, expected_version=task.version), data['report'])
        return data['report']


    @skill_request
    def handle_intent(self,event,claim,*,request=None,tasks=None):
        saved_reply = self._saved_answer_request(event, plan=(request or {}).get('plan'))
        if saved_reply is not None:
            return saved_reply
        if request and request.get('plan'):
            return self._plan_facts(event,request)
        return self.handle(event,claim,force=True,tasks=tasks)

    def _saved_answer_request(self,event,*,plan=None):
        text=event.content
        saved_only=bool(re.search(r'(?:只|仅)(?:看|用|使用|参考)(?:已保存|已有|现有)(?:的)?(?:资料|研究|结果)',text))
        reword=bool(re.search(r'重新整理|重新润色|润色.{0,8}(?:介绍|资料)|(?:介绍|回答).{0,8}(?:更丰富|更自然)|写得.{0,6}(?:丰富|自然)',text))
        evidence=bool(re.search(r'查看.{0,8}(?:研究原文|资料原文|景点依据|来源依据)|(?:这些|这份|景点).{0,8}(?:来源是什么|依据是什么)',text))
        if not (reword or evidence or saved_only) or (not saved_only and re.search(r'重新查询|重新搜索|最新|(?:只|仅)(?:看|用|使用|参考)(?![携程]*问道)|不要用问道',text)):
            return None
        row=next((r for r in self.store.research.latest(event) if has_provider(r) and r.get('report') and r['stage'] in {'completed','partial'}),None)
        if row is None:
            return '当前会话还没有可用的已保存景点资料。'
        if plan and plan_identity(row.get('plan_snapshot') or {}) != plan_identity(plan):
            return '已保存资料未绑定当前行程版本，不能直接用于本次介绍；请基于当前行程重新查询。'
        policy = source_selection(text, automatic=row.get('provider_policy', {}).get('requested', False),
                                  catalog=skill_asset('research.search_plan').resource('sources'))
        _, row['official_link_display_scope'] = bind_source_selection(policy,
            {**row.get('official_link_scope', {}), **row.get('target_scope', {})})
        if saved_only and not policy['only'] and policy['scope_overrides'].get('strict_sources'):
            return '已保存资料尚未按本次来源范围整理，请在原报告按来源查看；本次没有联网查询。'
        row['provider_policy'] = {**row.get('provider_policy', {}),
            'only':row.get('provider_policy', {}).get('only', False) or policy['only'], 'offline':policy['offline']}
        if row['provider_policy']['only']:
            row['sources'] = [source for source in row['sources'] if source.get('provider')=='ctrip_wendao']
            allowed_ids = {source['id'] for source in row['sources']}
            row['facts'] = [fact for fact in row.get('facts', []) if fact['source_id'] in allowed_ids]
            row.pop('answer_write', None)
        if saved_only:
            return answer_report(row)
        if evidence:
            view=presentation(row)
            matches=[e for e in view['entries'] if e['name'] in text]
            if matches:
                parts=[]
                for entry in matches:
                    excerpt=entry.get('source_excerpt')
                    if excerpt:
                        source=next(s for s in view['sources'] if s['id']==excerpt['source_id'])
                        parts.append('### '+entry['name']+'\n'+source.get('text','')[excerpt['start']:excerpt['end']])
                raw='\n\n'.join(dict.fromkeys(parts))
            else:
                raw='\n\n'.join(s.get('text','') for s in view['sources'] if s.get('provider')=='ctrip_wendao')
            return ('以下为已保存原文，资料取得时间：'+str(view.get('source_retrieved_at') or '未记录')+'。\n\n'+raw
                    if len(raw)<5600 else '原文较长，请在“查看完整报告”中切换“查看原文”阅读；查看不会重新查询。')
        existing=self.store.research.get(event)
        if existing and existing.get('answer_write'):
            return answer_report(existing)
        data=deepcopy(row)
        data.pop('answer_write',None)
        data.pop('research_id',None)
        data.update(query=text,source_reused_from=row.get('research_id'),request_counts={},
                    wendao_call={'status':'reused_saved_answer','physical_calls':0})
        if plan:
            data['plan_snapshot']={key:plan[key] for key in ('title','version','content','planning_spec','itinerary_snapshot') if key in plan}
            spec=plan.get('planning_spec') or {}
            if spec.get('start_date'):
                data['dates']={**data.get('dates',{}),'visit_start_date':spec['start_date'],
                    'visit_end_date':spec.get('end_date') or spec['start_date'],'date_is_tentative':bool(spec.get('date_is_tentative'))}
        self.store.research.save(event,'partial',data)
        return write_answer(self,event,data)

    def _plan_facts(self, event, request):
        """Bounded fact batches reuse per-event checkpoints and existing evidence rules."""
        selection = source_selection(source_request_text(event.content, request),
            automatic=self.wendao is not None and self.wendao.configured)
        if selection['offline']:
            return '没有发起联网搜索。'
        data = self.store.research.get(event) or {'query':event.content,'sources':[]}
        if data.get('scope_rejected'):
            return data['report']
        if data.get('report'):
            if data.get('wendao_processed') or has_provider(data):
                return answer_report(data)
            return attraction_report(data) if data.get('fact_schema') == 'attraction-facts-v1' else data['report']
        if data.get('target_scope'):
            if plan_identity(data.get('plan_snapshot') or {}) != plan_identity(request['plan']):
                return '本次研究绑定的行程版本已变化，已停止续查；请基于最新行程发起新请求。'
        if self.attraction_research is None and getattr(self.search,'configured',True) is False:
            if hasattr(self.store,'diagnostics'):
                self.store.diagnostics.record('routing',{'event_key':event.event_key,
                    'status':'research_rejected','reason':'search_not_configured'})
            return '网页搜索未配置，请在服务配置中设置 Tavily Key。'
        started = time.monotonic()
        writing_reserve = limits()[1]+10 if self.client is not None else 10
        def remaining_seconds():
            ensure_execution_active()
            scope = CURRENT_EXECUTION.get()
            remaining = FACT_TIME_SECONDS - (time.monotonic() - started)
            if scope:
                with self.store._connect() as db:
                    job = db.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?',(scope.job_id,)).fetchone()
                remaining = min(remaining,(datetime.fromisoformat(job['deadline_at'])-datetime.now(timezone.utc)).total_seconds())
            return remaining
        def available():
            return remaining_seconds() > writing_reserve+10
        available.remaining_seconds = remaining_seconds
        available.writing_reserve = writing_reserve
        try:
            if 'target_scope' not in data:
                continuation = bool(re.search(r'继续.{0,15}(?:剩下|未查|未返回|遗漏)|(?:查|查询).{0,6}(?:剩下|未查|漏掉)',event.content))
                if continuation:
                    from tools.research.evidence import answer_view
                    previous = next((r for r in self.store.research.latest(event) if r.get('report') and has_provider(r)
                        and r.get('plan_title')==request['plan']['title']),None)
                    if previous is None:
                        return '没有可可靠关联的上次查询范围，请明确要继续查询的景点。'
                    remaining = {e['name'] for e in answer_view(previous)['entries'] if e['retrieval_state']!='returned'}
                    targets = [t for t in previous['target_scope']['targets'] if t['name'] in remaining
                               and t['name'] in request['plan']['content']]
                    if not targets:
                        return '上次各景点均已取得对应资料；如需进一步核实，请明确具体字段或来源。'
                    changed_source = request.get('source_request') or re.search(r'(?:只|仅)(?:看|用|参考)|(?:用|不要|不用).{0,8}(?:问道|高德|官网|网站|搜索)',event.content)
                    if changed_source:
                        fresh = self.search_planner.plan_targets(request,request['plan'])
                        fresh['targets'] = [t for t in fresh['targets'] if t['name'] in remaining]
                        if not fresh['targets']:
                            return '未能将剩余景点与本轮来源要求可靠对应，尚未查询；请明确景点名称。'
                        data['target_scope'] = fresh
                    else:
                        data['target_scope'] = {**deepcopy(previous['target_scope']),'targets':targets,
                            'fields':{'query_text':request['query_text'],'topics':request.get('topics') or previous['target_scope']['fields']['topics']}}
                    data['continued_from_query_time'] = previous.get('updated_at')
                    if not changed_source:
                        request = {**request,'source_request':previous.get('query','')}
                else:
                    data['target_scope'] = (structured_target_scope(request, request['plan'])
                        or self.search_planner.plan_targets(request, request['plan']))
                data['dates'] = research_dates({**request,**data['target_scope']['fields']},event,
                                                request['plan']['duration_options'],
                                                planning_spec=request['plan'].get('planning_spec'))
                data['plan_title'] = request['plan']['title']
                data['plan_version'] = request['plan']['version']
                data['plan_snapshot'] = {key:request['plan'][key] for key in ('artifact_id','title','version','content','planning_spec','itinerary_snapshot') if key in request['plan']}
                self.store.research.save(event,'searching',data)
                if hasattr(self.store,'diagnostics'):
                    self.store.diagnostics.record('tool',{'event_key':event.event_key,
                        'name':'research_targets','status':'completed','prompt_version':'research-targets-v3',
                        'target_count':len(data['target_scope']['targets']),
                        'day_corrected_count':data['target_scope'].get('day_corrected_count',0),
                        'day_unresolved_count':data['target_scope'].get('day_unresolved_count',0),
                        'ignored_date_count':len(data['target_scope'].get('ignored_date_fields', []))})
        except (ValueError, KeyError, TypeError) as error:
            if hasattr(self.store,'diagnostics'):
                self.store.diagnostics.record('routing',{'event_key':event.event_key,
                    'status':'research_needs_input','reason':'invalid_target_scope'})
            return str(error) if isinstance(error,ValueError) else '未能可靠提取原方案景点，尚未搜索。'
        except ExecutionRevoked:
            raise
        except Exception as error:
            if hasattr(self.store,'diagnostics'):
                self.store.diagnostics.record('tool',{'event_key':event.event_key,
                    'name':'research_targets','status':'failed','error_type':type(error).__name__})
            return '研究模型暂不可用，尚未提取景点或发起搜索，请稍后重试。'
        scope = data['target_scope']
        if self.attraction_research is not None:
            return self.attraction_research.run(event, request, data, available, selection=selection)
        targets, topics = scope['targets'],scope['fields']['topics']
        batches = data.setdefault('fact_batches',[])
        for start in range(0,min(len(targets),FACT_BATCHES*FACT_BATCH_SIZE),FACT_BATCH_SIZE):
            batch_index = start // FACT_BATCH_SIZE
            if len(batches) <= batch_index:
                batches.append({'start':start,'searches':[],'sources':[]})
            batch = batches[batch_index]
            if batch.get('done'):
                continue
            if not available():
                break
            group = targets[start:start+FACT_BATCH_SIZE]
            for offset,target in enumerate(group):
                if len(batch['searches']) > offset:
                    continue
                if not available():
                    break
                day = self._target_date(target,data['dates'])
                # Only extracted public names/date/topics go to the search provider.
                labels = ' '.join(TOPICS[t] for t in topics) if scope['language'].startswith('zh') else 'admission price opening hours reservation rules'
                query = ' '.join((request['plan']['destination'],target['name'],day,labels,
                                  '官方网站 游客须知' if scope['language'].startswith('zh') else 'official visitor information'))
                kwargs = {'language':scope['language']}
                if scope['requested_domains']:
                    kwargs['include_domains'] = scope['requested_domains']
                if scope['excluded_domains']:
                    kwargs['exclude_domains'] = scope['excluded_domains']
                try:
                    rows = self.search.search(self._query(query),**kwargs)
                    rows = [r for r in rows if self._domain_allowed(r['url'],scope['requested_domains'],scope['excluded_domains'])]
                    batch['searches'].append({'results':self._select([rows],2)})
                except (ValueError,OSError):
                    batch['searches'].append({'results':[],'error':'search_unavailable'})
                self.store.research.save(event,'searching',data)
            if len(batch['searches']) < len(group):
                break
            batch['results'] = self._select([s['results'] for s in batch['searches']],4)
            # One read checkpoint per page. Check the soft budget before starting a new page.
            while len(batch['sources']) < len(batch['results']) and available():
                one = {'results':batch['results'][:len(batch['sources'])+1], 'sources':batch['sources']}
                self._read_sources(event,one,checkpoint=data)
                source = batch['sources'][-1]
                if not self._domain_allowed(source['url'],scope['requested_domains'],scope['excluded_domains']):
                    source.update(status='unavailable',text='',access_error='redirect_outside_source_policy')
                    self.store.research.save(event,'reading',data)
            if len(batch['sources']) < len(batch['results']) or not available():
                break
            batch['coverage'] = self._fact_synthesis(group,topics,batch['sources'],data['dates'])
            batch['done'] = True
            self.store.research.save(event,'synthesizing',data)
        data['report'] = self._fact_report(data)
        covered = sum(c['status'] in {'evidence_found','date_pending'} for b in batches for c in b.get('coverage',[]))
        total = len(targets)*len(topics)
        completed = covered == total and all(c['status']=='evidence_found' for b in batches for c in b.get('coverage',[]))
        data['coverage'] = {'target_count':len(targets),'topic_count':len(topics),'covered_count':covered,
                            'remaining_count':total-covered,'batch_count':sum(bool(b.get('done')) for b in batches)}
        # The existing Web research panel reads this bounded source index.
        data['sources'] = [{**s,'id':i+1} for i,s in enumerate(s for b in batches for s in b['sources'])]
        self.store.research.save(event,'completed' if completed else 'partial',data)
        if hasattr(self.store,'diagnostics'):
            self.store.diagnostics.record('routing',{'event_key':event.event_key,
                'status':'research_completed' if completed else 'research_partial',**data['coverage']})
        return data['report']

    @staticmethod
    def _target_date(target, dates):
        start = dates.get('visit_start_date')
        if start and target.get('day_index'):
            return (date.fromisoformat(start)+timedelta(days=target['day_index']-1)).isoformat()
        return ''

    @skill_request
    def _fact_synthesis(self, targets, topics, sources, dates):
        """Validate each target/topic independently; one bad claim cannot fill coverage."""
        if not self.client or not sources:
            return []
        try:
            with model_call_context('research_fact_synthesis',prompt_version='research-facts-v1', stage_id='research.facts'):
                response = self.client.chat.completions.create(model=self.model,response_format={'type':'json_object'},messages=[
                    {'role':'system','content':(skill_text('research.facts', 'system') + f'{MAX_ITEMS}' + skill_text('research.facts', 'system_2') + f'{MAX_TEXT_CHARS}' + skill_text('research.facts', 'system_3'))},
                    {'role':'user','content':complete_json({'targets':[{'index':i+1,**t,'visit_date':self._target_date(t,dates)} for i,t in enumerate(targets)],
                        'topics':topics,'dates':dates,'sources':[{**{k:s.get(k,'') for k in ('id','title','url','status')},
                             'text':s.get('text','')[:EXCERPT_CHARS]} for s in sources]},30000)}])
            value = response_json(response)
            rows = value.get('coverage')
            if not isinstance(rows,list) or len(rows)>MAX_ITEMS:
                return []
            result, seen = [],set()
            from tools.research.evidence import source_applicability
            for row in rows:
                try:
                    index, topic = row['target_index'],row['topic']
                    if type(index) is not int or not 1<=index<=len(targets) or topic not in topics or (index,topic) in seen:
                        continue
                    item = validate_research_items([row['item']],sources)[0]
                    refs = item.get('evidence',[])
                    conflict = row.get('conflict',False)
                    if type(conflict) is not bool or (conflict and len({r['source_id'] for r in refs})<2):
                        continue
                    if (not refs or any(targets[index-1]['name'] not in ref['quote'] for ref in refs)
                            or any(s['status']!='read' for s in sources if s['id'] in item['sources'])):
                        continue
                    quote = '\n'.join(ref['quote'] for ref in refs)
                    topic_words = {'ticket_price':r'票|价格|免费|元|¥|admission|ticket|free|cost',
                                   'opening_hours':r'开放|营业|开馆|闭馆|闭园|open|close|hours',
                                   'reservation_rules':r'预约|预订|预定|reservation|booking|book|reserve'}
                    if not re.search(topic_words[topic],quote,re.I):
                        continue
                    if re.search(r'未知|未公布|待定|暂未公布|尚未明确|未提供',quote):
                        continue
                    day = self._target_date(targets[index-1],dates)
                    states = [source_applicability(s,date.fromisoformat(day) if day else None)['date_status']
                              for s in sources if s['id'] in item['sources']]
                    status = ('conflict' if conflict or any(s in {'expired','not_yet_effective','conflicting_range'} for s in states) else
                              'evidence_found' if day and states and all(s=='within_declared_window' for s in states) else 'date_pending')
                    result.append({'target_index':index,'topic':topic,'status':status,'item':item})
                    seen.add((index,topic))
                except (ValueError,KeyError,TypeError,IndexError):
                    continue
            return result
        except ExecutionRevoked:
            raise
        except Exception as error:
            if hasattr(self.store,'diagnostics'):
                self.store.diagnostics.record('tool',{'name':'research_fact_synthesis','status':'failed',
                    'error_type':type(error).__name__,'error_code':'fact_synthesis_unavailable'})
            return []

    def _fact_report(self, data):
        scope, dates = data['target_scope'],data['dates']
        lines = ['## 原方案景点事实核验', '关联方案：'+data['plan_title'],
                 '选源策略：优先检索景点官网公开说明；已读正文仅表示可引用，来源官方身份仍需核对。']
        if dates.get('arrival_date'):
            lines.append('抵达日期：'+dates['arrival_date'])
        if dates.get('visit_start_date'):
            lines.append('游览日期：'+dates['visit_start_date']+(' 至 '+dates['visit_end_date'] if dates.get('visit_end_date') else '（总天数待明确）'))
        if dates.get('year_inferred'):
            lines.append('年份按消息时间和上海时区解析；“号”承接明确月份。以上仅为研究日期背景。')
        for index,target in enumerate(scope['targets']):
            batch = next((b for b in data.get('fact_batches',[]) if b['start']<=index<b['start']+FACT_BATCH_SIZE),{})
            coverage = {c['topic']:c for c in batch.get('coverage',[]) if c['target_index']==index-batch.get('start',0)+1}
            tags = ('；第'+str(target['day_index'])+'天' if target.get('day_index') else '；游览日待确认')
            tags += ('；可选/备选' if target['optional'] else '') + ('；仅外观，不等同购票入内' if target['visit_mode']=='exterior' else '')
            lines.append('\n### '+target['name']+tags)
            for topic in scope['fields']['topics']:
                row = coverage.get(topic)
                if row is None:
                    lines.append('- '+TOPICS[topic]+'：未知；'+('尚未完成该批核验。' if not batch.get('done') else '本批未取得可通过核验的正文引文。'))
                    continue
                label = {'evidence_found':'已找到正文依据（官方身份待核对）','date_pending':'已有引文，目标日期适用性待确认','conflict':'来源规则冲突、日期冲突或不适用于目标日期'}[row['status']]
                lines.append('- '+TOPICS[topic]+'：'+label+'。'+row['item']['text'])
                for ref in row['item'].get('evidence',[]):
                    source = next(s for s in batch['sources'] if s['id']==ref['source_id'])
                    url = source['url'].replace(')','%29').replace('(','%28')
                    lines.append('  依据：'+ref['quote']+' [来源]('+url+')；检索时间：'+source['retrieved_at'])
        lines.append('\n本次按有限批次核验；未知项和适用日期待确认项仍需后续核对。未找到依据不代表免费、无需预约或停止开放。研究没有修改行程、创建提醒或执行预订。')
        remaining = [t['name'] for i,t in enumerate(scope['targets']) if not any(
            b.get('done') and b['start']<=i<b['start']+FACT_BATCH_SIZE for b in data.get('fact_batches',[]))]
        if remaining:
            lines.append('尚未完成核验的景点：'+'、'.join(remaining)+'。可继续指定其中的景点分批查询。')
        return '\n'.join(lines)

    def _search(self, event, data, query):
        if getattr(self.search,'configured',True) is False:
            raise ValueError('网页搜索未配置，请在服务配置中设置 Tavily Key；也可提供公开 HTTPS 页面链接。')
        if 'search_plan' not in data:
            data['search_plan'] = self.search_planner.plan(query)
            previous = self.store.research.context(event)
            plan = data['search_plan']
            if (previous and previous['destination'] == plan.get('destination')
                    and not plan.get('requested_domains') and not plan.get('excluded_domains')):
                preferred = previous['preferred_domains']
                if preferred:
                    first = plan['batches'][0]
                    remaining = [b for b in plan['batches'] if not set(b['domains']) & set(preferred)]
                    plan['batches'] = ([{**first,'domains':preferred}] if previous['strict_sources'] else
                                       [{**first,'domains':preferred},*remaining][:3])
                    plan['requested_domains'] = preferred
                    plan['strict_sources'] = previous['strict_sources']
                    plan['note'] = '继续优先本会话先前指定的平台。' + plan['note']
            self.store.research.update_context(event,data['search_plan'])
            self.store.research.save(event,'searching',data)
        plan = data['search_plan']
        if not plan['batches']:
            raise ValueError(plan['note'])
        batches = data.setdefault('search_batches',[])
        for batch in plan['batches'][len(batches):]:
            ensure_execution_active()
            try:
                kwargs = {}
                if batch['domains']: kwargs['include_domains'] = batch['domains']
                if plan['excluded_domains']: kwargs['exclude_domains'] = plan['excluded_domains']
                if batch.get('language'): kwargs['language'] = batch['language']
                results = self.search.search(self._query(batch['query']),**kwargs)
                # Enforce domain constraints even if a provider returns an unexpected URL.
                results = [r for r in results if self._domain_allowed(r['url'],batch['domains'],plan['excluded_domains'])]
                batches.append({'results':results})
            except (ValueError,OSError) as error:
                batches.append({'results':[],'error':str(error)[:160]})
            self.store.research.save(event,'searching',data)
        results = self._select([b['results'] for b in batches],6)
        if len(results)<3 and any(b['domains'] for b in plan['batches']) and not plan['strict_sources']:
            extra = self._fallback(event,data,query)
            results = self._select([results,extra],6)
        if not results and any(b.get('error') for b in batches):
            raise ValueError('搜索来源暂时不可用，请稍后重试或提供公开链接。')
        return results

    def _fallback(self, event, data, query):
        if data.get('fallback_done'):
            return data.get('fallback_results',[])
        try:
            excluded = data['search_plan']['excluded_domains']
            kwargs = {'exclude_domains':excluded} if excluded else {}
            first = data['search_plan']['batches'][0]
            if first.get('language'):
                kwargs['language'] = first['language']
                query = first['query']
            rows = self.search.search(query,**kwargs)
            data['fallback_results'] = [r for r in rows if self._domain_allowed(r['url'],[],excluded)]
        except (ValueError,OSError):
            data['fallback_results'] = []
        data['fallback_done'] = True
        data['search_plan']['note'] += ' 优先来源不足或正文不可读，已补充其他公开检索结果。'
        self.store.research.save(event,'searching',data)
        return data['fallback_results']

    @staticmethod
    def _domain_allowed(url, included, excluded):
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        matches = lambda domain: host == domain or host.endswith('.'+domain)
        return (parsed.scheme=='https' and bool(host) and not parsed.username and not parsed.password
                and not any(matches(d) for d in excluded) and (not included or any(matches(d) for d in included)))

    @staticmethod
    def _select(batches, limit):
        # Round-robin preserves each source group's opportunity; cap repeated hosts.
        result, seen, counts = [],set(),{}
        for index in range(max((len(b) for b in batches),default=0)):
            for batch in batches:
                if index >= len(batch): continue
                row = batch[index]
                url = row['url'].split('#')[0]
                host = (urlsplit(url).hostname or '').removeprefix('www.')
                if url in seen or counts.get(host,0)>=2: continue
                seen.add(url); counts[host]=counts.get(host,0)+1
                result.append(row)
                if len(result)==limit: return result
        return result

    def _read_sources(self, event, data, *, checkpoint=None):
        hashes = {s.get('content_hash') for s in data['sources'] if s.get('content_hash')}
        for row in data['results'][len(data['sources']):]:
            ensure_execution_active()
            snippet = row['snippet'] if not public_text_issue(row['snippet']) else ''
            source = {**row,'id':len(data['sources'])+1,'status':'snippet','text':snippet,
                      'retrieved_at':datetime.now(timezone.utc).isoformat()}
            try:
                scope = (checkpoint or data).get('target_scope') or data.get('search_plan', {})
                allowed = lambda url: self._domain_allowed(url,
                    scope.get('requested_domains', []) if scope.get('strict_sources') else [],
                    scope.get('excluded_domains', []))
                source.update(self.reader(row['url'], url_allowed=allowed))
                if source.get('content_hash') in hashes:
                    source.update(status='duplicate',text='重复正文，不作为独立证据。')
                hashes.add(source.get('content_hash'))
            except (ValueError,OSError) as error:
                source['status'] = 'snippet' if snippet else 'unavailable'
                source['access_error'] = str(error)[:160]
            data['sources'].append(source)
            from tools.research.evidence import source_applicability
            source['applicability'] = source_applicability(source)
            self.store.research.save(event,'reading',checkpoint if checkpoint is not None else data)

    @staticmethod
    def _query(text):
        # No memory, files, IDs or history are included. Refuse obvious pasted credentials.
        if re.search(r'(?i)api.?key|bearer\s|sk-[A-Za-z0-9]|tvly-', text):
            raise ValueError('搜索请求中疑似包含凭据，请删除后重试。')
        return text[:500]

    _planning_source = staticmethod(planning_source)

    @skill_request
    def _summarize(self, query, sources, preferences=None, *, strategy_note='', evidence_sink=None):
        usable = [s for s in sources if s['status'] in ('read', 'snippet') and s['text']]
        lines = ['网页研究结果（当前会话）']
        if strategy_note:
            lines.append('选源策略：'+strategy_note)
        if not usable:
            lines.append('未取得可用内容，不能据此核实攻略。请更换公开页面或稍后重试。')
        elif self.client:
            try:
                with model_call_context('research_synthesis', prompt_version='research-synthesis-v4', stage_id='research.synthesis'):
                    response = self.client.chat.completions.create(model=self.model, response_format={'type': 'json_object'}, messages=[
                        {'role': 'system', 'content': (skill_text('research.synthesis', 'system') + f'{MAX_ITEMS}' + skill_text('research.synthesis', 'system_2') + f'{TARGET_TEXT_CHARS}' + skill_text('research.synthesis', 'system_3') + f'{MAX_TEXT_CHARS}' + skill_text('research.synthesis', 'system_4'))},
                        {'role': 'user', 'content': complete_json({'request': query, 'preference_defaults': preferences or {},
                            'preference_rule': skill_text('research.synthesis', 'preference_rule'),
                            'evidence': [{k: s.get(k, '') for k in ('id', 'title', 'url', 'status', 'published_at','access_error')} |
                                {'text':s['text'][:EXCERPT_CHARS], 'original_chars':len(s['text']),
                                 'excerpted':len(s['text'])>EXCERPT_CHARS} for s in usable]},45000)}])
                value = response_json(response)
                items = validate_research_items(value['items'], usable)
                if evidence_sink is not None:
                    evidence_sink.extend(items)
                for item in items:
                    lines.append('- ' + item['text'] + ' ' + ''.join(f'[{i}]' for i in item['sources']))
                    refs = item.get('evidence', [])
                    if refs and item['text'] == SOURCE_PREFIX + refs[0]['quote']:
                        refs = refs[1:]
                    lines.extend(f"  依据[{ref['source_id']}]：{ref['quote']}" for ref in refs)
            except (ValueError, KeyError, TypeError, IndexError) as error:
                diagnostics = getattr(self.store, 'diagnostics', None)
                if diagnostics is not None:
                    diagnostics.record('tool', {'name': 'research_synthesis', 'status': 'failed',
                        'error_code': error.code if isinstance(error, ResearchEvidenceError) else 'invalid_response'})
                lines.append('模型整理未通过引用校验或关键事实核验，以下仅展示来源摘录。')
                lines.extend(f"- [{s['id']}] {s['text'][:220]}" for s in usable)
        else:
            lines.append('未配置模型，以下为来源摘录，尚未综合核实：')
            lines.extend(f"- [{s['id']}] {s['text'][:220]}" for s in usable)
        lines.append('\n来源：')
        labels = {'read': '已读正文', 'snippet': '仅搜索摘要', 'unavailable': '读取失败', 'duplicate': '重复正文'}
        for s in sources:
            title = re.sub(r'[\[\]()<>\n\r]', '', s['title']) or urlsplit(s['url']).hostname
            lines.append(f"[{s['id']}] [{title}]({s['url'].replace(')', '%29').replace('(', '%28')}) — {labels[s['status']]}；发布时间：{s.get('published_at') or '未知'}；检索：{s['retrieved_at']}")
            if s.get('applicability'):
                state=s['applicability']['date_status']
                label={'expired':'已超过来源声明有效期','not_yet_effective':'尚未到来源声明生效日',
                       'conflicting_range':'来源声明的日期范围冲突','within_declared_window':'参考日在来源声明区间内'}.get(state,'有效期未明确提供')
                lines.append('  适用性：'+label+'；目标出游日及同名实体仍需核对。')
        lines.append('\n开放时间、预约和价格需核对目标日期的有效说明；搜索结果不代表已订票。可说“根据研究结果规划武汉三天行程”。')
        lines.append('公开页面读取不包含登录后或仅动态加载的内容；仅有搜索摘要的来源不代表已获取全文。')
        return '\n'.join(lines)



    def web_reports(self, event):
        from tools.research.evidence import (attraction_report, has_provider, presentation, render, fingerprint, WRITTEN_ANSWER_VERSION as ANSWER_VERSION)
        # No full source bodies in progress responses.
        rows = self.store.research.latest(event)
        from tools.research.evidence import has_provider
        from tools.research.evidence import presentation, render, fingerprint, WRITTEN_ANSWER_VERSION as ANSWER_VERSION
        result = []
        for row in rows:
            view = presentation(row) if (has_provider(row) or row.get('official_links')) and row.get('report') else None
            identity_key = fingerprint([(s.get('content_hash'),s.get('retrieved_at')) for s in row.get('sources',[])])[:16]
            item = {'stage':row['stage'],'updated_at':row['updated_at'], 'research_id':fingerprint(row['research_id'])[:16] if row.get('research_id') else identity_key,
                    'report':render(view,detailed=True) if view else
                        attraction_report(row,detailed=True) if row.get('fact_schema')=='attraction-facts-v1' and row.get('report') else row.get('report',''),
                    'source_count':len(row.get('sources',[]))}
            if view:
                item['presentation'] = {**view,'replayed_saved_answer':view.get('cache_reprocessed',False) or row.get('answer_schema')!=ANSWER_VERSION}
            item['content_version'] = fingerprint(item)
            result.append(item)
        return result

    def apply_intent(self,event,claim,ir,*,mode='execute',tasks=None):
        def diagnostic(status, **details):
            if hasattr(self.store, 'diagnostics'):
                self.store.diagnostics.record('routing', {'event_key':event.event_key,
                    'status':status,'schema_version':SCHEMA_VERSION, **details})
        operations=[op for op in ir.operations if op.domain=='research' and op.operation=='search_travel_guides']
        if len(operations)!=1 or len(ir.operations)!=1 or ir.action not in {'read','create','clarify'} or not ir.source_spans:
            diagnostic('research_rejected', reason='invalid_research_action')
            return '这次攻略研究的目标还不明确，请说明目的地和想参考的来源。'
        try:
            values, ignored = project_request(operations[0].values, event.content)
        except ResearchRequestError as error:
            diagnostic('research_rejected', reason=error.code)
            return '研究请求的核心字段无法对应你的原话，尚未搜索；请确认查询目标、方案引用和日期。'
        request = {**values, 'original_request':event.content}
        if values.get('plan_reference') or values.get('plan_index'):
            request.setdefault('topics', ['reservation_rules'] if re.search(r'官网|官方网站', event.content) else list(TOPICS))
        if values.get('plan_reference') or values.get('plan_index'):
            plan, resolution = resolve_plan(self.store, event, values)
            if plan is None:
                diagnostic('research_needs_input', plan_resolution=resolution)
                return '请确认要核验哪份方案（可引用方案序号或标题）；尚未搜索，也未修改原行程。'
            request['plan'] = plan
            request['plan_resolution'] = resolution
        try:
            request['dates'] = research_dates(request, event, request.get('plan',{}).get('duration_options',[]),
                                             planning_spec=request.get('plan',{}).get('planning_spec'))
        except ValueError as error:
            diagnostic('research_needs_input', reason='invalid_research_dates')
            return str(error)
        if mode=='preview':
            return '已识别联网攻略研究请求；当前仅预览，尚未搜索。'
        diagnostic('research_accepted', adopted_fields=sorted(values), ignored_fields=ignored,
                   plan_resolution=request.get('plan_resolution','not_referenced'),
                   scope_kind='saved_trip' if request.get('plan', {}).get('itinerary_snapshot')
                       else 'plan_artifact' if request.get('plan') else 'general_query',
                   plan_version=request.get('plan', {}).get('version'))
        return self.handle_intent(event,claim,request=request,tasks=tasks)

