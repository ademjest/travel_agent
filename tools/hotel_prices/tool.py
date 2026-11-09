"""The single request-driven executor for independent and itinerary hotel reference prices."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import re
from zoneinfo import ZoneInfo

from core.context_contract import complete_json, response_json
from core.execution_scope import ensure_execution_active, ExecutionRevoked
from core.model_budget import model_call_context,ModelBudgetExceeded
from infrastructure.skill_manager import skill_text, skill_request
from infrastructure.wendao_client import WendaoError

from tools.hotel_prices.quotes import (VERSION, EVIDENCE_VERSION, STAY_FIELDS, fingerprint, normalized, store_name, public_stay,
    validate_quote_facts, project_quote, reusable, evaluate, amount, lodging_target, target_key, target_matches, unique_targets, unique_places,candidate_choices)
from core.hotel_contract import (evidenced_request, resolve_stay, missing_request, leaves, LABELS, HotelEvidenceError,
    merge_hotel_state, candidate_transition, date_proposal, short_proposal_reply, selection_number, stay_text, hotel_task_views, history_field_matches,NEW_RUN_COMMAND,RUN_LIMIT_CODES)
from core.tool_result import ToolResult
from core.time_input import NUMBER

def reference_text(snapshot):
    """Show compact source-backed clues; long source text belongs to explicit reading."""
    clues=snapshot.get('reference_clues',[])
    if clues:
        lines=['问道参考价格（价格口径、门店或产品条件待核对，不用于预算通过或可订判断）：']
        def cell(value):return str(value or '未明确').replace('|',r'\|').replace('\n',' ')
        for hotel in dict.fromkeys(c['hotel_name'] for c in clues):
            lines.extend(['\n**'+cell(hotel)+'**','', '| 房型 | 原文参考价格 | 早餐 | 退改 |', '| --- | --- | --- | --- |'])
            for clue in (c for c in clues if c['hotel_name']==hotel):
                qualifier='约 ' if clue.get('amount_qualifier')=='approximate' else ''
                prices='；'.join(n['date']+'：'+qualifier+n['amount']+' '+clue['currency']+'/间/晚' for n in clue.get('nightly_prices',[]))
                if clue.get('reference_amounts'):prices='来源展示金额：'+qualifier+'、'.join(clue['reference_amounts'])+' '+clue['currency']+'（计价口径待核对）'
                if clue.get('reported_total') is not None:prices+=('；' if prices else '')+'原文总价 '+qualifier+clue['reported_total']+' '+clue['currency']
                for field,label in (('from_price','起价'),('average_price','均价')):
                    if clue.get(field) is not None:prices+=('；' if prices else '')+label+' '+qualifier+clue[field]+' '+clue['currency']
                lines.append('| '+' | '.join(cell(v) for v in [clue['room_name'],prices,clue.get('breakfast'),clue.get('cancellation')])+' |')
        reasons=list(dict.fromkeys(reason for clue in clues for reason in clue.get('unresolved_reasons',[])))
        lines.extend(['',*reasons])
        if any(c.get('taxes_and_fees_status')!='included' for c in clues):lines.append('税费及完整应付金额待确认。')
        lines.append('以上仅为供应商参考，不保证库存或成交价；完整条款可从“查看原文”读取。')
        if snapshot.get('unavailable_note'):lines.extend(['',snapshot['unavailable_note']])
        return '\n'.join(lines)
    if snapshot.get('unavailable_note'):return snapshot['unavailable_note']
    if snapshot.get('raw_text'):
        return '已取得供应商资料，但尚未形成可核验的结构化报价；请从“查看原文”查看完整内容。'
    return ''


def quote_text(spec,snapshot,now=None,*,places=None):
    snapshot=project_quote(snapshot)
    if not snapshot.get('retrieved_at') and not snapshot.get('raw_text'):
        heading='报价查询未取得可用结果' if snapshot.get('request_id') else '住宿报价尚未查询'
        return heading+'。\n'+(snapshot.get('note') or '保留住宿要求与候选，房价待核对。')
    offers=evaluate(spec,snapshot,now,places=places);lines=[]
    try:stamp=datetime.fromisoformat(snapshot['retrieved_at'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
    except (KeyError,ValueError):stamp='未知'
    lines.append('问道参考报价 · 查询时间 '+stamp+'（北京时间；不保证库存或成交价）')
    if snapshot.get('note'):lines.append(snapshot['note'])
    if snapshot.get('is_trial'):lines.append('按本次查询条件试算，不代表已完成预订或更改行程。')
    stay=snapshot.get('stay',{})
    children=stay.get('children_ages')
    children_text='无儿童' if children==[] else '、'.join(str(age)+'岁' for age in children) if isinstance(children,list) else '尚未明确'
    if stay.get('check_in'):lines.append(f"查询条件：{stay['check_in']}入住、{stay.get('check_out','未知')}退房，{stay.get('rooms','未知')}间房、{stay.get('adults','未知')}名成人；{children_text}。")
    for item in offers:
        qualifier='约 ' if item.get('amount_qualifier')=='approximate' else ''
        prices='、'.join(n['date']+'：'+qualifier+n['amount']+' '+item['currency']+'/间/晚' for n in item['nightly_prices']) or '逐晚价格未知'
        if item['price_basis']=='from':prices='起价线索（不代表所需产品可得价）：'+prices
        elif item['price_basis'] in {'average','unknown'}:prices='价格口径待核对：'+prices
        if item.get('reported_total') is not None:prices+='；供应商全程总价 '+('约 ' if item.get('amount_qualifier')=='approximate' else '')+item['reported_total']+' '+item['currency']
        if item.get('calculated_total') is not None:prices+='；本地逐晚加总 '+qualifier+item['calculated_total']+' '+item['currency']
        if item.get('average_price') is not None:prices+='；供应商均价 '+qualifier+item['average_price']+'（不能据此验证每晚）'
        if item.get('from_price') is not None:prices+='；起价 '+qualifier+item['from_price']+'（不能据此确认所需产品）'
        if item.get('budget_difference') is not None and amount(item['budget_difference'])>0:
            prices+='；列示金额'+('高于上限' if item['numeric_comparison']=='over' else '低于下限')+item['budget_difference']+'元'
        conclusion='适用已列入住条件，未据此判定预算达标' if item['status']=='reference_available' else '完整口径参考价在预算范围，仍需下单前核对'
        lines.append('- '+item['hotel_name']+' · '+item['room_name']+'：'+prices+'；'+'；'.join(item['issues'] or [conclusion]))
    references=snapshot.get('reference_clues',[])
    if references:lines.append('\n'+reference_text(snapshot)+'\n')
    elif not offers and snapshot.get('raw_text') and snapshot['result_note']!=snapshot.get('note'):lines.append(snapshot['result_note'])
    elif snapshot.get('unavailable_note'):lines.append(snapshot['unavailable_note'])
    covered={o['amap_poi_id'] for o in offers}|{c['requested_target_ref'] for c in references}|{p['requested_target_ref'] for p in snapshot.get('unavailable_prices',[])}
    missing_names=[name for pid,name in snapshot.get('requested_hotels',{}).items() if pid not in covered]
    if missing_names:lines.append('仍待核对门店或产品：'+'、'.join(missing_names)+'。')
    if snapshot.get('rejected'):
        if not references:lines.append('核对说明：'+'；'.join(list(dict.fromkeys(r['reason'] for r in snapshot['rejected']))[:3]))
    if offers and not any(o['status'] in {'reference_match','reference_available'} for o in offers):lines.append('当前没有可用于完整预算匹配的已核实报价；不表示没有房源，也不代表其他未返回产品。')
    if not offers and snapshot.get('source_record_id'):lines.append('完整原文已保存，可从“查看原文”读取，不会重新查询。')
    return '\n'.join(lines)


class HotelQuotes:
    def __init__(self,store,wendao,client,model,*,amap=None,enabled=True,clock=None):
        self.store,self.wendao,self.client,self.model=store,wendao,client,model
        self.amap=amap
        self.enabled=enabled;self.clock=clock or (lambda:datetime.now(timezone.utc))

    def presentation(self,event,source_event,content):
        quotes=self.store.research.hotel_quotes_for_event(event,source_event)
        if len(quotes)!=1:return None
        quote=quotes[0]
        if quote.get('source_event_id')!=source_event or quote.get('evidence_version')==EVIDENCE_VERSION:return None
        # Only replace a pure hotel tool reply, never a combined answer or an explicit original read.
        with self.store._connect() as db:
            receipts=db.execute("SELECT result_text FROM event_tool_results WHERE event_id=? AND tool_name='query_hotel_prices'",(source_event,)).fetchall()
        if not any(json.loads(row[0]).get('data')==content for row in receipts):return None
        spec={'destination':quote['city'],'hotel_stay':quote['stay']}
        return {'text':'已保存资料 · 本地重新整理，非新查询\n\n'+quote_text(spec,quote,self.clock(),places=quote.get('targets'))}

    def actions(self,event,task,source_event):
        actions=[]
        quotes=self.store.research.hotel_quotes_for_event(event,source_event)
        for quote in quotes:
            if quote.get('raw_text'):
                label='查看原文' if len(quotes)==1 else '、'.join(quote.get('requested_hotels',{}).values())+' · '+quote.get('stay',{}).get('check_in','')+' · '+str(quote.get('retrieved_at',''))
                actions.append({'kind':'view_original','label':label,'source_event_id':quote['source_event_id'],
                    'revision':fingerprint(quote['raw_text']),
                    **({'reference_text':reference_text(quote)} if source_event==quote['source_event_id'] and not quote.get('offers') and not quote.get('evidence_version') else {})})
        if task and task.task_type=='read_batch':
            for view in hotel_task_views(task):actions.extend(a for a in self.actions(event,view,source_event) if a['kind']!='view_original')
            return actions
        if not task or task.last_event_id!=source_event or task.status!='collecting' or task.is_expired(datetime.now(timezone.utc)):return actions
        state=merge_hotel_state(task.slots,{})
        common={'task_id':task.task_id,'task_version':task.version}
        if 'hotel_query_index' in state:common['query_index']=state['hotel_query_index']
        if state.get('selection_status') in {'awaiting','invalid'} and 'targets' in task.missing_slots:
            for i,p in enumerate(state.get('hotel_candidates',[]),1):
                actions.append({**common,'kind':'select','label':f"{i}. {p['name']} · {p.get('address','')}",
                    'candidate_set_id':state['candidate_set_id'],'candidate_set_version':state['candidate_set_version'],
                    'candidate_id':p['id'],'selectable':lodging_target(p)})
        proposal=date_proposal(state.get('hotel_date_proposal'))
        if proposal.get('status')=='proposed' and proposal.get('presented'):
            for kind,label in (('accept_dates','使用这组日期'),('reject_dates','修改日期')):
                actions.append({**common,'kind':kind,'label':label,'proposal_id':proposal['proposal_id'],'proposal_version':proposal['version']})
        if 'history_record' in task.missing_slots:
            for quote in self.history(event,{}):
                if quote.get('raw_text'):
                    actions.append({'kind':'view_original','label':'、'.join(quote.get('requested_hotels',{}).values())+' · '+quote.get('stay',{}).get('check_in',''),
                        'source_event_id':quote['source_event_id'],'revision':fingerprint(quote['raw_text'])})
        return actions

    def proposal_current(self,event,proposal):
        if not proposal.get('trip_id'):return True
        trip=self.store.trips.get(event,proposal['trip_id'])
        if not trip or trip.get('status')!='active':return False
        drafts=self.store.trips.current_drafts(event,trip['trip_id'])
        current={'trip':trip['version'],'draft':drafts[0].version if drafts else None}
        version=proposal.get('source_version')
        return current==version if isinstance(version,dict) else trip['version']==version and not drafts

    def validate_action(self,event,action):
        task=self.store.tasks.owned(event,action['task_id'])
        if not task or task.status!='collecting' or task.version!=action['task_version'] or task.is_expired(datetime.now(timezone.utc)):
            raise ValueError('这项酒店选择已过期或任务已变化，请使用最新回复。')
        views=hotel_task_views(task)
        task=next((v for v in views if v.slots.get('hotel_query_index')==action.get('query_index')),None)
        if task is None:raise ValueError('这项操作不属于当前酒店查询。')
        state=merge_hotel_state(task.slots,{})
        if action['kind']=='select':
            if (state.get('selection_status') not in {'awaiting','invalid'} or action.get('candidate_set_id')!=state.get('candidate_set_id')
                    or action.get('candidate_set_version')!=state.get('candidate_set_version')):
                raise ValueError('候选列表已变化，请使用最新列表重新选择。')
            candidates=state.get('hotel_candidates',[])
            ordinal=next((i for i,p in enumerate(candidates,1) if p['id']==action.get('candidate_id') and lodging_target(p)),None)
            if ordinal is None:raise ValueError('该选项不是当前可选择的住宿门店。')
            values={'reference':{'ordinal':ordinal,'selection':event.content}};state['hotel_selection_ordinal']=ordinal
        else:
            proposal=date_proposal(state.get('hotel_date_proposal'))
            if (proposal.get('status')!='proposed' or not proposal.get('presented') or proposal.get('proposal_id')!=action.get('proposal_id') or proposal.get('version')!=action.get('proposal_version')):
                raise ValueError('日期提议已变化，请先核对最新日期。')
            if not self.proposal_current(event,proposal):raise ValueError('行程版本已变化，请重新确认日期提议。')
            values={'proposal_response':{'kind':'stay_dates','decision':'accept' if action['kind']=='accept_dates' else 'reject','source':event.content}}
        return replace(task,slots=state),values

    def local_selection(self,event,tasks):
        ordinal=selection_number(event.content)
        decision=short_proposal_reply(event.content)
        named=[]
        for task in [v for t in tasks for v in hotel_task_views(t)]:
            if task.status!='collecting' or 'targets' not in task.missing_slots:continue
            state=merge_hotel_state(task.slots,{})
            choices=candidate_choices(state,event.content)
            if choices:named.append((task,state,choices))
        if named:
            if len(named)!=1 or len(named[0][2])!=1:return '有多个候选列表或同名门店匹配，请引用对应列表或使用绑定按钮选择，本次没有查询。'
            task,state,choices=named[0];ordinal=choices[0]
            return replace(task,slots={**state,'hotel_selection_ordinal':ordinal}),{'reference':{'ordinal':ordinal,'selection':event.content[:240]}}
        if ordinal is None and decision is None:return None
        if ordinal is None and re.search(r'[，,；;\n]',event.content):return None  # Preserve compound field updates for the semantic contract.
        if ordinal is not None and not event.reply_to_id:
            latest=self.store.tasks.latest_message_event(event)
            if latest and len(self.store.research.hotel_quotes_for_event(event,latest))>1:
                return '当前显示的是多份酒店资料记录，请引用需要的候选列表或使用对应原文按钮，本次没有查询。'
        eligible=[]
        for task in [v for t in tasks for v in hotel_task_views(t)]:
            if task.task_type!='hotel' or task.status!='collecting':continue
            state=merge_hotel_state(task.slots,{})
            if ordinal is not None and 'targets' in task.missing_slots and not set(task.missing_slots)&{'rooms','adults','children_ages'} and state.get('selection_status') in {'awaiting','invalid'} and state.get('hotel_candidates'):
                eligible.append(replace(task,slots={**state,'hotel_selection_ordinal':ordinal}))
            proposal=date_proposal(state.get('hotel_date_proposal'))
            if ordinal is None and proposal.get('status')=='proposed' and proposal.get('presented'):eligible.append(replace(task,slots=state))
        if not eligible:return None
        if len(eligible)!=1 or any(t.status=='collecting' and t.task_id!=eligible[0].task_id for t in tasks):
            return '有多个待答事项，请引用要选择的酒店列表或日期提议，本次没有查询。'
        if ordinal is not None and not 1<=ordinal<=len(eligible[0].slots.get('hotel_candidates',[])):
            eligible[0]=replace(eligible[0],slots={**eligible[0].slots,'hotel_invalid_selection':True})
            return eligible[0],{'reference':{'selection':event.content}}
        values=({'reference':{'ordinal':ordinal,'selection':event.content}} if ordinal is not None else
                {'proposal_response':{'kind':'stay_dates','decision':decision,'source':event.content}})
        return eligible[0],values

    def original(self,event,source_event,revision='',*,cursor=0,max_chars=4000):
        quote=self.store.research.hotel_quote_for_event(event,source_event)
        if not quote or not quote.get('raw_text'):raise ValueError('原文不存在、已删除或不属于当前会话。')
        raw=quote['raw_text'];digest=fingerprint(raw)
        if revision and revision!=digest:raise ValueError('原文版本不匹配，请重新打开资料。')
        if type(cursor) is not int or not 0<=cursor<=len(raw):raise ValueError('原文分页位置无效。')
        return {'text':raw[cursor:cursor+max_chars],'cursor':cursor,'next_cursor':cursor+max_chars if cursor+max_chars<len(raw) else None,
            'total_chars':len(raw),'source_event_id':source_event,'revision':digest,'historical':True,'queried_at':quote.get('retrieved_at')}

    def local_read(self,event):
        match=re.fullmatch(r'查看酒店报价原文(?:\s*第(\d+)页)?[。！？?]*',event.content.strip())
        record=re.fullmatch(r'查看酒店报价记录第(\d+)条原文[。！？?]*',event.content.strip())
        if not match and not record and event.content.strip() not in {'查看酒店报价记录','查看酒店历史报价'}:return None
        source=(event.message_reference or {}).get('event_id')
        if not source and event.reply_to_id:source=self.store.tasks.message_events(event,[event.reply_to_id]).get(event.reply_to_id)
        quotes=self.store.research.hotel_quotes_for_event(event,source) if source else self.history(event,{})
        if record:
            index=int(record[1])-1
            if not source or not 0<=index<len(quotes):return '请引用对应酒店资料列表，并提供该列表内的记录序号；本次未联网。'
            quotes=[quotes[index]]
        if self.store.delivery.get_event_status(event.event_key) is not None:
            result=ToolResult('completed','query_hotel_prices',payload={'offers':[],'historical':True,
                'history_refs':[q['source_event_id'] for q in quotes[:20]]})
            self.store.delivery.save_event_tool_result('hotel-local:'+event.event_key,event.event_key,'hotel_local_read',{},result.to_json())
        if not (match or record) or len(quotes)!=1:
            return '本地酒店资料记录（未调用模型或供应商）：\n'+self.history_index(event,quotes)+'\n可使用原文按钮，或引用本条记录列表后发送“查看酒店报价记录第1条原文”。'
        quote=quotes[0];page=int(match[1] or 1) if match else 1
        try:data=self.original(event,quote['source_event_id'],cursor=(page-1)*4000)
        except ValueError as error:return str(error)
        return '已保存的问道原文（历史资料，本次未联网）\n'+data['text']+f"\n第{page}页 · 共{data['total_chars']}字符"+('；引用本条回复后可发送“查看酒店报价原文 第'+str(page+1)+'页”。' if data['next_cursor'] is not None else '；原文已读完。')

    def execute_result(self,name,arguments,context):
        from tools.research.sources import planning_policy
        from core.commands import social_message
        event=context.event
        if event is None:return ToolResult('failed',name,error_code='missing_context',data='酒店查价缺少消息上下文。')
        if social_message(event.content) and not (arguments.get('proposal_response') and short_proposal_reply(event.content)):
            return ToolResult('failed',name,error_code='operation_denied',data='本轮没有酒店查询或补充请求，未联网。')
        data=merge_hotel_state(context.execution_data or {},{});known=deepcopy(data.get('hotel_request',{}))
        if data.get('hotel_invalid_selection') and arguments.get('reference') and 'ordinal' not in arguments['reference']:
            return ToolResult('needs_input',name,data='序号超出当前酒店列表范围，请重新选择。',missing_fields=('targets',),
                payload={'request':known,'selection_status':'invalid','selected_candidate_ids':[]})
        if not data.get('hotel_collecting'):known.pop('mode',None)
        proposal=date_proposal(data.get('hotel_date_proposal',{}))
        response=arguments.get('proposal_response')
        if response and (not response.get('source') or response['source'] not in event.content):
            return ToolResult('needs_input',name,data='日期答复缺少本轮原话依据，请重新确认当前日期提议。',missing_fields=('check_in','check_out'),
                payload={'request':known,'date_proposal':proposal})
        decision=response.get('decision') if response else short_proposal_reply(event.content) if proposal.get('status')=='proposed' else None
        if decision:
            local=short_proposal_reply(event.content)
            reference=event.message_reference or {}
            shown=[date_proposal((r.get('payload') or {}).get('date_proposal')) for r in reference.get('tool_results',[]) if r.get('action')=='query_hotel_prices']
            stale=bool(data.get('hotel_expired') or not self.proposal_current(event,proposal) or
                reference.get('event_id') and not any(p.get('proposal_id')==proposal.get('proposal_id') for p in shown))
            if local and local!=decision:
                return ToolResult('needs_input',name,data='日期提议的接受或拒绝意思尚不一致，请重新确认；尚未查询。',missing_fields=('check_in','check_out'),payload={'request':known})
            if not proposal or proposal.get('status')!='proposed' or not proposal.get('presented') or stale:
                return ToolResult('needs_input',name,data='没有唯一有效的当前日期提议，或行程版本已经变化，请重新确认入住退房日期。',
                    missing_fields=('check_in','check_out'),payload={'request':known,'date_proposal':{**proposal,'status':'stale'}})
            proposal['status']='accepted' if decision=='accept' else 'declined'
            if decision=='accept':known={**known,'stay':{**known.get('stay',{}),**{k:proposal[k] for k in ('check_in','check_out')}}}
            else:known={**known,'stay':{k:v for k,v in known.get('stay',{}).items() if k not in {'check_in','check_out'}}}
        reference=event.message_reference or {};source_event=reference.get('event_id','')
        linked=self.store.research.hotel_quotes_for_event(event,source_event) if source_event else []
        quoted=linked[0] if len(linked)==1 else None
        prior=[r['payload'] for r in reference.get('tool_results',[]) if r.get('action')=='query_hotel_prices'
               and isinstance(r.get('payload'),dict) and isinstance(r['payload'].get('request'),dict)]
        if arguments.get('reference') and not quoted and len(prior)==1 and prior[0].get('target_candidates'):
            old=prior[0]
            if not data.get('hotel_candidates'):
                current_scope={k:known[k] for k in ('targets','discovery') if known.get(k)}
                prior_scope={k:old['request'][k] for k in ('targets','discovery') if old['request'].get(k)}
                if not current_scope or current_scope==prior_scope:
                    restored={**old['request'],**known,'stay':{**old['request'].get('stay',{}),**known.get('stay',{})}}
                    candidates=old['target_candidates']
                    return ToolResult('needs_input',name,data='已从引用消息恢复原候选列表，日期和人数按当前已确认条件保留；请重新选择具体门店，尚未查询。\n'+
                        '\n'.join(f"{i}. {p['name']} · {p.get('address','')}"+('（不可选）' if not lodging_target(p) else '') for i,p in enumerate(candidates,1)),
                        missing_fields=('targets',),payload={'request':restored,'target_candidates':candidates,
                            'resolved_targets':data.get('hotel_targets',{}) if current_scope else {},
                            'target_bindings':data.get('hotel_target_bindings',{}) if current_scope else {},
                            **candidate_transition(data,candidates,restored,event.event_key)})
            same=(old.get('candidate_set_id')==data.get('candidate_set_id') if old.get('candidate_set_id') else
                [p['id'] for p in old['target_candidates']]==[p['id'] for p in data.get('hotel_candidates',[])])
            if not same:
                return ToolResult('needs_input',name,data='引用的酒店列表已变化，请按最新列表重新选择。',missing_fields=('targets',),
                    payload={'request':known,'selection_status':'invalid','selected_candidate_ids':[]})
        if not quoted and len(prior)==1:
            known={**prior[0]['request'],**known,'stay':{**prior[0]['request'].get('stay',{}),**known.get('stay',{})}}
            data={**data,'hotel_targets':data.get('hotel_targets',prior[0].get('resolved_targets',{}))}
            if not data.get('hotel_collecting'):known.pop('mode',None)
        if source_event and not quoted and not prior and arguments.get('reference'):
            return ToolResult('needs_input',name,data='引用中没有可访问的酒店资料，请提供门店或引用相应报价。',missing_fields=('targets',))
        bound=deepcopy(data.get('hotel_targets',{}))
        if quoted:
            bound=quoted.get('targets',{})
            data={**data,'hotel_candidates':[],'selection_status':'selected'}
            known={**known,'targets':[{'name':p['name'],'city':quoted['city']} for p in bound.values()],
                   'stay':quoted.get('stay',{})}
        policy=planning_policy(data.get('source_context',{}),event.content,automatic=True)
        choices=candidate_choices(data,event.content,arguments.get('targets')) if arguments.get('targets') and not quoted else []
        if choices:
            if len(choices)!=1:
                return ToolResult('needs_input',name,data='有多个同名候选符合这些条件，请明确分店地址或使用候选按钮选择；原入住条件已保留。',
                    missing_fields=('targets',),payload={'request':known})
            if arguments.get('mode',known.get('mode','lookup'))=='saved' or policy.get('offline'):
                known={**known,'targets':deepcopy(arguments['targets'])}
            else:
                arguments={k:v for k,v in arguments.items() if k!='targets'}
                arguments['reference']={'ordinal':choices[0],'selection':event.content[:240]}
                data['hotel_selection_ordinal']=choices[0]
        try:
            request=evidenced_request(arguments,data.get('hotel_evidence') or event.content,previous=known,
                reference_time=data.get('reference_time') or event.occurred_at or self.clock(),selection=data.get('hotel_selection_ordinal'))
        except HotelEvidenceError as error:
            self.store.diagnostics.record('routing',{'event_key':event.event_key,'status':'hotel_needs_input',
                'validation':{'code':error.code,'path':error.path}})
            fields=tuple(dict.fromkeys(row['path'].split('.')[1] if row['path'].startswith('stay.')
                else row['path'].split('.')[0] for row in error.issues))
            provenance=deepcopy(data.get('field_provenance',{}));old_fields=dict(leaves(known))
            for path,value in leaves(error.request):
                if old_fields.get(path,object())!=value:
                    provenance[path]={'event_id':event.event_key,'source':value if isinstance(value,str) else event.content[:200]}
            return ToolResult('needs_input',name,error_code=error.code,
                data=str(error)+'\n已保留其余可核对条件；补充后继续核价，尚未调用供应商。',missing_fields=fields,
                payload={'request':error.request,'resolved_targets':bound,'source_policy':policy,
                    'field_provenance':provenance,
                    'date_proposal':proposal,'validation_errors':error.issues,'historical':arguments.get('mode')=='saved' or bool(policy.get('offline')),
                    **({'selection_status':'invalid','selected_candidate_ids':[]} if any(r['path'].startswith('reference.') for r in error.issues) else {})})
        except ValueError as error:
            if event.attachments:
                return ToolResult('needs_input',name,data='截图中的门店和入住条件尚未确认。请补充完整酒店名、城市及入住条件；截图价格不会作为当前API报价。',
                    missing_fields=('targets',),payload={'request':known,'unverified_targets':arguments.get('targets',[])})
            return ToolResult('failed',name,error_code='invalid_argument',data=str(error))
        provenance=deepcopy(data.get('field_provenance',{}));old_fields=dict(leaves(known))
        for path,value in leaves(arguments):
            if old_fields.get(path,object())!=value:
                provenance[path]={'event_id':event.event_key,'source':value if isinstance(value,str) else event.content[:200],
                                  'reference_time':data.get('reference_time') or (event.occurred_at or self.clock()).isoformat()}
        mode=request.get('mode','lookup')
        request.pop('proposal_response',None)
        anchor={**{k:proposal[k] for k in ('check_in','check_out') if k in proposal},**known.get('stay',{})}
        reference_time=data.get('reference_time') or event.occurred_at or self.clock()
        if mode=='saved' and quoted:
            reference_time=quoted.get('field_provenance',{}).get('stay.check_in',{}).get('reference_time') or quoted.get('recorded_at') or quoted.get('retrieved_at') or reference_time
        request['stay']=resolve_stay(request.get('stay',{}),reference_time,event.content,anchor)
        if policy.get('offline') and mode=='refresh':
            return ToolResult('failed',name,error_code='source_restricted',data='当前不允许联网，尚未重新查价。',payload={'request':request,'source_policy':policy})
        if mode=='saved' or policy.get('offline'):
            filters={k:request[k] for k in ('targets',) if k in arguments}
            if 'stay' in arguments:
                filters['stay']={k:v for k,v in request['stay'].items() if k in arguments['stay']}
                for key,value in arguments['stay'].items():
                    if key in {'check_in','check_out'} and re.fullmatch('(?:'+NUMBER+r'月)?'+NUMBER+r'[日号]',value) and not quoted:
                        filters['stay'][key]=value
                invalid=[key for key in arguments['stay'] if key in {'check_in','check_out'} and key not in filters['stay']]
                if invalid:
                    return ToolResult('needs_input',name,error_code='unresolved_hotel_date',data='历史日期条件尚未核对，请明确日期或选择已保存记录；本次未联网。',missing_fields=tuple(invalid),
                        payload={'historical':True,'offers':[],'request':known,'history_refs':[q['source_event_id'] for q in self.history(event,{})[:20]]})
            matches=self.history(event,filters,source_event_id=quoted.get('source_event_id') if quoted else source_event)
            if len(matches)!=1:
                text='当前有多份匹配的酒店历史资料，请选择原文记录；本次没有联网。' if matches else '规范化条件未命中已保存资料；本次没有联网。'
                available=matches or self.history(event,{})
                index=self.history_index(event,available)
                return ToolResult('needs_input' if matches else 'completed',name,data=text+'\n'+index,
                    missing_fields=('history_record',) if matches else (),payload={'request':known,'offers':[],'historical':True,'source_policy':policy,
                        'history_refs':[q['source_event_id'] for q in available[:20]]})
            result=self._result(name,filters,matches[0],historical=True,raw='原文' in event.content)
            return replace(result,payload={**result.payload,'source_policy':policy})
        items=deepcopy(request.get('targets',[]) if 'targets' in arguments else data.get('hotel_request_items',request.get('targets',[])))
        request['targets']=unique_targets(request.get('targets',[])) if request.get('targets') else []
        if 'targets' in arguments:request.pop('discovery',None)
        elif 'discovery' in arguments and arguments['discovery']!=known.get('discovery'):request.pop('targets',None);items=[]
        changed=any(key in arguments and request.get(key)!=known.get(key) for key in ('targets','discovery'))
        bindings=deepcopy(data.get('hotel_target_bindings',{}))
        if changed:
            bound={};bindings={};data={**data,'selection_status':'stale'}
        for target in request.get('targets',[]):
            matches=[pid for pid,p in bound.items() if store_name(p['name'],target.get('city',''))==store_name(target.get('name'),target.get('city',''))]
            if len(matches)==1:bindings.setdefault(target_key(target),matches[0])
        missing=missing_request(request,self.clock())
        ordinal=None
        if request.get('reference'):
            ordinal=request['reference'].get('ordinal')
            if not ordinal and re.search(r'便宜|最低价',request['reference'].get('selection','')):
                return ToolResult('needs_input',name,data='请明确酒店序号；只有日期、房型及税费等口径可比较且唯一最低时才能确定最便宜，不能按不完整价格猜选。',missing_fields=('targets',),payload={'request':request,'resolved_targets':bound})
            if re.search(r'下一批',request['reference'].get('selection','')):
                remaining=data.get('hotel_remaining_targets',[])
                if remaining:
                    request['targets']=deepcopy(remaining);bound={};request.pop('reference')
                    missing=missing_request(request,self.clock())
                elif data.get('hotel_batch_continuation'):
                    request.pop('reference');missing=missing_request(request,self.clock())
                else:
                    return ToolResult('needs_input',name,data='没有可绑定的剩余酒店批次，请引用原报价或提供本次名单。',missing_fields=('targets',),payload={'request':request})
            choices=data.get('hotel_candidates') or list(bound.values())
            if ordinal and (data.get('selection_status')=='stale' or not 1<=ordinal<=len(choices) or not lodging_target(choices[ordinal-1])):
                return ToolResult('needs_input',name,data='该选择无效、已失效或不是住宿门店，请按当前列表重新选择。' if choices else '原候选列表不可用，请引用此前的候选消息以恢复，或补充完整门店名；已验证的日期和人数仍保留。',missing_fields=('targets',),
                    payload={'request':{k:v for k,v in request.items() if k!='reference'},'selection_status':'stale' if changed else 'invalid','selected_candidate_ids':[]})
            if ordinal:
                selected=choices[ordinal-1]
                keys=selected.get('candidate_for',[]) if data.get('hotel_candidates') else []
                if data.get('hotel_candidates') and not keys:
                    pending_keys=[target_key(t) for t in request.get('targets',[]) if target_key(t) not in bindings]
                    keys=pending_keys if len(pending_keys)==1 else [target_key(request['targets'][0])] if len(request.get('targets',[]))==1 else []
                    if not keys:
                        return ToolResult('needs_input',name,data='旧列表无法明确对应多组酒店目标，请补充本次选择的完整门店名；原列表和条件已保留。',missing_fields=('targets',),payload={'request':known})
                if keys:
                    scope={target_key(t):t for t in request.get('targets',[])}
                    if any(key not in scope or not target_matches(selected,scope[key]) for key in keys):
                        return ToolResult('needs_input',name,data='所选门店不符合当前城市或地址限定，请按当前范围重新选择。',missing_fields=('targets',),
                            payload={'request':known,'selection_status':'invalid','selected_candidate_ids':[]})
                    for key in keys:bindings[key]=selected['id']
                    bound[selected['id']]=selected
                    bound={pid:p for pid,p in bound.items() if pid in bindings.values()}
                else:
                    bound={selected['id']:selected};request['targets']=[{'name':selected['name'],'city':selected.get('cityname','')}]
                request.pop('discovery',None);missing=[f for f in missing if f!='targets']
                request.pop('reference',None)
            elif not bound and request.get('reference') and not request.get('targets'):
                missing=list(dict.fromkeys([*missing,'targets']))
            elif ordinal:
                missing=list(dict.fromkeys([*missing,'targets']))
        payload={'request':request,'resolved_targets':bound,'field_provenance':provenance,'date_proposal':proposal,
                 'request_items':items,
                 'source_policy':policy,'unfinished':missing,'target_bindings':bindings,
                 **({'selection_status':'stale','selected_candidate_ids':[]} if changed else {}),
                 **({'selection_status':'selected','selected_candidate_ids':list(bound)} if request.get('reference') is None and ordinal else {})}
        if data.get('hotel_expired') and not re.search(r'继续|重新核价|重新查询',event.content):
            return ToolResult('needs_input',name,data='原酒店待补任务已过期，已保留门店和这次补充条件。是否继续核价？',
                missing_fields=('continue_query',),payload={**payload,'expired':True})
        if data.get('hotel_expired'):payload['expired']=False
        if missing:
            known_stay=request.get('stay',{})
            details=stay_text(known_stay)
            proposed=proposal.get('status')=='proposed' and not known_stay.get('check_in')
            if proposed:proposal['presented']=True
            needed=[k for k in missing if not proposed or k not in {'check_in','check_out'}]
            return ToolResult('needs_input',name,data=('已知'+details+'。\n' if details else '')+
                ('是否按'+proposal['check_in']+'入住、'+proposal['check_out']+'退房？' if proposed else '')+
                ('请一并补充：'+'、'.join(LABELS.get(k,k) for k in needed)+'。' if needed else '')+'补齐后继续核价，不会保存或预订。',
                missing_fields=tuple(missing),payload=payload)
        if request['stay'].get('rooms')!=1:
            return ToolResult('failed',name,error_code='unsupported_occupancy',data='已保留多房需求；首期仅支持单间连续住宿，尚未提交查询。',payload=payload)
        if not self.enabled or not self.wendao or not self.wendao.configured:
            return ToolResult('failed',name,error_code='not_available',data='酒店报价服务未启用或未配置，尚未查询。',payload=payload)
        if not policy.get('requested'):
            return ToolResult('failed',name,error_code='source_restricted',data='当前来源限制不允许查询问道酒店价格，尚未查询。',payload=payload)
        requested=request.get('targets',[]);cities={t.get('city') for t in requested if t.get('city')}
        if any(re.match(r'https?://',t.get('name','')) for t in requested):
            return ToolResult('needs_input',name,data='酒店链接尚不能唯一核实具体分店，请补充完整门店名与城市；未把链接或截图价当作当前报价。',missing_fields=('targets',),payload=payload)
        if len(cities)>1:
            return ToolResult('needs_input',name,data='这些门店属于不同城市，请选择本次同城查询，其他目标保留待后续批次。',missing_fields=('targets',),payload=payload)
        city=next(iter(cities),request.get('discovery',{}).get('city',''))
        if not city:
            return ToolResult('needs_input',name,data='请补充酒店所在城市，避免同名分店串价。',missing_fields=('targets',),payload=payload)
        requested=requested[:3]
        bindings={key:pid for key,pid in bindings.items() if pid in bound and lodging_target(bound[pid])}
        if not bound and requested and not any(t.get('address_hint') for t in requested) and not data.get('hotel_candidates'):
            cached=self.saved(event,request)
            if cached and normalized(cached.get('city',''))==normalized(city) and reusable(cached,request['stay'],self.clock()):
                bound={pid:p for pid,p in cached.get('targets',{}).items() if lodging_target(p)}
        for target in requested:
            matches=[pid for pid,p in bound.items() if store_name(p['name'],city)==store_name(target.get('name'),city)]
            if len(matches)==1:bindings[target_key(target)]=matches[0]
        pending=[target for target in requested if target_key(target) not in bindings]
        candidates=[];map_calls=0
        existing=data.get('hotel_candidates',[]) if data.get('selection_status')!='stale' else []
        if pending and existing:
            candidates=existing
        elif pending or not requested and not bound:
            if not policy.get('map_allowed') or not self.amap:
                return ToolResult('failed',name,error_code='identity_unverified',data='当前无法使用地图核实具体门店身份；尚未提交报价查询。',payload=payload)
            try:
                if requested:
                    for target in pending:
                        rows=unique_places([p for p in self.amap.search_places(city,target.get('name','')) if p.get('address') and target_matches(p,target)]);map_calls+=1
                        exact=[p for p in rows if store_name(p.get('name'),city)==store_name(target.get('name'),city)]
                        if len(exact)==1:
                            place=exact[0];bound[place['id']]=place;bindings[target_key(target)]=place['id']
                        elif len(rows)==1 and rows[0]['id'] in bound:
                            bindings[target_key(target)]=rows[0]['id']
                        else:
                            for place in rows:
                                old=next((p for p in candidates if p['id']==place['id']),None)
                                if old:old['candidate_for'].append(target_key(target))
                                else:candidates.append({**place,'candidate_for':[target_key(target)]})
                else:
                    from tools.hotel_prices.catalog import hotel_brand_targets,match_hotel_brand
                    discovery=request['discovery'];brands=hotel_brand_targets(discovery)
                    if discovery.get('preferred_groups') and not brands:
                        return ToolResult('needs_input',name,data='该集团暂未在已核对品牌目录中唯一对应，请补充品牌或具体门店。',missing_fields=('targets',),payload=payload)
                    for keyword in [b['name'] for b in brands][:2] or ['酒店']:
                        rows=self.amap.search_places(city,discovery.get('area','')+' '+keyword);map_calls+=1
                        for place in rows:
                            if (lodging_target(place) and place.get('address') and normalized(place.get('cityname','')).removesuffix('市')==normalized(city).removesuffix('市')
                                    and (not brands or match_hotel_brand(place['name'],brands))):bound[place['id']]=place
                    bound=dict(list(bound.items())[:3])
            except Exception as error:
                return ToolResult('failed',name,error_code='place_resolution_failed',data='门店身份核验未完成，尚未提交房价查询。',payload={**payload,'resolved_targets':bound,'target_bindings':bindings,'map_calls':map_calls,'error_type':type(error).__name__})
            payload.update(target_candidates=candidates[:12],**candidate_transition(data,candidates[:12],request,event.event_key))
        pending=[target for target in requested if target_key(target) not in bindings]
        payload.update(resolved_targets=bound,target_bindings=bindings,map_calls=map_calls)
        if pending or not bound:
            text=('门店未能唯一绑定，请选择具体分店：\n'+'\n'.join(f"{i}. {p['name']} · {p.get('address','地址未知')}" for i,p in enumerate(candidates[:12],1))) if candidates else '未找到可选酒店，请补充完整门店名、城市或地址限定；尚未提交房价查询。'
            return ToolResult('needs_input',name,data=text,missing_fields=('targets',),payload=payload)
        if not all(lodging_target(p) for p in bound.values()):
            return ToolResult('needs_input',name,data='所选对象不是可核验住宿门店，请重新选择。',missing_fields=('targets',),payload={**payload,'selection_status':'invalid','selected_candidate_ids':[]})
        if any(not target_matches(bound[bindings[target_key(t)]],t) for t in requested):
            return ToolResult('needs_input',name,data='门店不符合当前查询范围，请重新核对城市和地址。',missing_fields=('targets',),payload={**payload,'selection_status':'invalid','selected_candidate_ids':[]})
        payload.update(selection_status='selected',selected_candidate_ids=list(bound))
        coverage={'input_items':len(items),'unique_requested':len(unique_targets(items)),'unique_hotels':len(bound),'batch_input_items':len(requested)}
        mapping=[{'input_index':i,'target':t,'poi_id':bindings.get(target_key(t))} for i,t in enumerate(items)]
        snapshot=self.lookup(event,{'city':city,'stay':request['stay'],'budget':request.get('budget'),'field_provenance':provenance,
            'request_coverage':coverage,'input_mapping':mapping,
            'unfinished':[t['name'] for t in request.get('targets',[])[3:]]},targets=bound,policy=policy,refresh=mode=='refresh')
        snapshot.update(targets=bound,query_request=request,map_calls=map_calls,
            unfinished=[t['name'] for t in request.get('targets',[])[3:]])
        result=self._result(name,request,snapshot)
        result=replace(result,payload={**result.payload,'request_coverage':coverage,'input_mapping':mapping,
            **{k:v for k,v in payload.items() if k not in {'request','resolved_targets','unfinished'}}})
        if data.get('hotel_previous_status')=='failed':
            result=replace(result,data='上一次酒店查询未取得可用结果；本次按这条新消息继续核价。\n'+result.data)
        return result

    def _result(self,name,request,snapshot,*,historical=False,raw=False):
        snapshot=project_quote(snapshot)
        spec={'destination':snapshot.get('city',''),'hotel_stay':snapshot.get('stay',{}),
              'budgets':{'accommodation':request['budget']} if request.get('budget') else {}}
        text=quote_text(spec,snapshot,self.clock(),places=snapshot.get('targets'))
        if historical:text='以下为已保存的历史参考资料，本次未联网。\n'+text
        if snapshot.get('reused'):text='复用相同条件的本地参考资料。\n'+text
        if snapshot.get('unfinished'):text+='\n尚未查询：'+'、'.join(snapshot['unfinished'])+'；请在后续消息继续下一批。'
        if snapshot.get('raw_text'):
            excerpt=snapshot['raw_text'][:3500]
            text+=('\n\n### 已保存的问道原文\n'+excerpt+('\n（原文节选；完整资料编号：'+snapshot.get('source_record_id','')+'）' if len(excerpt)<len(snapshot['raw_text']) else '') if raw else '\n可回复“查看酒店报价原文”读取已保存资料，不会重新查询。')
        code=snapshot.get('error_code','')
        blocked=snapshot.get('budget_block',{})
        return ToolResult('needs_input' if blocked else 'failed' if code else 'completed',name,data=text,error_code=code,retryable=False,
            missing_fields=('model_budget',) if blocked else (),
            artifact_id=snapshot.get('source_record_id',''),payload={'request':request,'snapshot':snapshot,
            'resolved_targets':snapshot.get('targets',{}),'offers':evaluate(spec,snapshot,self.clock()),'reference_clues':snapshot.get('reference_clues',[]),
            'result_status':snapshot['result_status'],'result_note':snapshot['result_note'],
            'field_provenance':snapshot.get('field_provenance',{}),'source_policy':snapshot.get('provider_policy',{}),
            'remaining_targets':request.get('targets',[])[3:],
            'historical':historical,'cache_hit':bool(snapshot.get('reused')),'unfinished':snapshot.get('unfinished',[]),'budget_block':blocked})

    def saved(self,event,request,*,source_event_id=''):
        matches=self.history(event,request,source_event_id=source_event_id)
        return matches[0] if matches else None

    def history(self,event,request,*,source_event_id=''):
        matches=[];seen=set()
        for record in self.store.research.hotel_quote_records(event,limit=None):
            snapshot=record.get('extraction',{}).get('snapshot')
            if not snapshot:continue
            if source_event_id and snapshot.get('source_event_id')!=source_event_id:continue
            names={normalized(t['name']) for t in request.get('targets',[]) if t.get('name')}
            if names and not names<={normalized(n) for n in snapshot.get('requested_hotels',{}).values()}:continue
            if any(not history_field_matches(k,snapshot.get('stay',{}).get(k),v) for k,v in request.get('stay',{}).items() if k in STAY_FIELDS):continue
            if names:
                snapshot=deepcopy(snapshot)
                ids={pid for pid,name in snapshot.get('requested_hotels',{}).items() if normalized(name) in names}
                snapshot['offers']=[o for o in snapshot.get('offers',[]) if o['amap_poi_id'] in ids]
                snapshot['reference_clues']=[c for c in snapshot.get('reference_clues',[]) if c['requested_target_ref'] in ids]
                snapshot['unavailable_prices']=[p for p in snapshot.get('unavailable_prices',[]) if p['requested_target_ref'] in ids]
                for field in ('requested_hotels','targets'):snapshot[field]={pid:p for pid,p in snapshot.get(field,{}).items() if pid in ids}
                snapshot['requested_poi_ids']=list(ids)
            key=fingerprint({'hotels':snapshot.get('requested_hotels'),'stay':snapshot.get('stay'),'raw':snapshot.get('raw_text')})
            if key not in seen:matches.append(deepcopy(snapshot));seen.add(key)
        return matches

    def history_index(self,event,snapshots):
        return ('\n'.join(f"{i}. {'、'.join(s.get('requested_hotels',{}).values())} · {s.get('stay',{}).get('check_in','日期未知')}至{s.get('stay',{}).get('check_out','未知')} · 查询时间{s.get('retrieved_at','未知')}" for i,s in enumerate(snapshots[:20],1))
            +('；仅展示最近20条，可引用对应历史消息读取更早原文。' if len(snapshots)>20 else '')) if snapshots else '当前会话没有可访问的酒店原文记录。'

    @skill_request
    def lookup(self,event,request,*,targets,policy,refresh=False,allow_query=True):
        now=self.clock();requested=list(targets)[:3];targets={pid:targets[pid] for pid in requested}
        spec={'destination':request.get('city',''),'hotel_stay':{k:v for k,v in request.get('stay',{}).items() if k in STAY_FIELDS},
              'budgets':{'accommodation':request['budget']} if request.get('budget') else {}}
        missing=missing_request({'targets':[{'name':p['name'],'city':spec['destination']} for p in targets.values()], 'stay':spec['hotel_stay']},now)
        base={'schema_version':1,'provider':'ctrip_wendao','contract_version':VERSION,'city':spec.get('destination'),
              'offers':[],'stay':deepcopy(spec.get('hotel_stay',{})),'provider_policy':policy,
              'is_trial':bool(request.get('is_trial')),'source_trip_id':request.get('source_trip_id'),'note':''}
        if policy.get('source_restricted') or policy.get('offline'):return {**base,'note':'当前来源限制不使用问道报价；保留酒店与预算要求，房价待核对。','error_code':'source_restricted'}
        if missing:return {**base,'note':('请一并补充：' if allow_query else '指定日期报价尚缺：')+'、'.join(LABELS.get(k,k) for k in missing)
            +('。补充后继续核价，不直接保存行程。' if allow_query else '。需要查价时可补充，不默认采用1间房、1名成人。'),'missing_fields':missing}
        if not targets:return {**base,'note':'没有与当前指定名称匹配的已绑定酒店候选，未借用其他分店报价。'}
        if any(not lodging_target(p) for p in targets.values()):
            return {**base,'error_code':'invalid_lodging_target','note':'目标类别不是可核验住宿门店，未提交报价查询。'}
        if any(not p.get('address') or normalized(p.get('cityname','')).removesuffix('市')!=normalized(spec['destination']).removesuffix('市') for p in targets.values()):
            return {**base,'note':'门店城市或地址尚未核对，未提交报价查询。'}
        key=fingerprint({'version':VERSION,'city':spec['destination'],'stay':{k:base['stay'].get(k) for k in sorted(STAY_FIELDS)},
                         'targets':{pid:{k:p.get(k) for k in ('name','address','cityname','location')} for pid,p in targets.items()}})
        if not refresh:
            latest=self.saved(event,{'targets':[{'name':p['name']} for p in targets.values()],'stay':base['stay']})
            if latest and not allow_query:return {**latest,'reused':True}
            snapshots=[r.get('extraction',{}).get('snapshot',{}) for r in self.store.research.hotel_quote_records(event)]
            for snapshot in snapshots:
                if snapshot.get('request_fingerprint')==key and reusable(snapshot,base['stay'],now):
                    return {**snapshot,'reused':True}
        if not allow_query:
            return {**base,'note':'尚无适用的已保存酒店报价，本轮仅更新本地要求，未联网查价。'}
        if not self.enabled or not self.wendao or not self.wendao.configured or not policy['requested']:
            return {**base,'note':'酒店报价未启用、问道不可用或来源限制不允许；保留候选，房价待核对。','error_code':'not_available'}
        query='只查询酒店参考报价，不预订不下单。以下是公开住宿条件，不要推断缺失字段。每家最多两个产品，分别列明确适用的入住退房日期、人数房数、每晚日期价格及币种单位、总价是否含全部税费、早餐退改及会员优惠限制、完整门店名地址和携程链接；未知明确写未知。不要计算预算是否达标。\n'+json.dumps(
            {'city':spec['destination'],'stay':public_stay(base['stay']),'hotels':[{'target_id':pid,'name':p['name'],'address':p['address']} for pid,p in targets.items()]},ensure_ascii=False)
        try:result=self.wendao.query(event,self.store.research,query,purpose='hotel_quotes',fingerprint=key)
        except (WendaoError,ValueError) as error:return {**base,'note':str(error),'error_code':'provider_rejected'}
        snapshot={**base,'request_fingerprint':key,'request_id':fingerprint([event.event_key,'hotel_quotes']),
                  'targets':deepcopy(targets),'query_request':deepcopy(request),'unfinished':request.get('unfinished',[]),
                  'field_provenance':request.get('field_provenance',{}),
                  'source_event_id':event.event_key,'content_hash':result.get('content_hash'),'retrieved_at':result.get('retrieved_at'),
                  'recorded_at':result.get('submitted_at'),
                  'requested_poi_ids':requested,'requested_hotels':{pid:p['name'] for pid,p in targets.items()},
                  'input_mapping':request.get('input_mapping',[]),
                  'coverage':{'asked':len(targets),'returned':0,'bound':0,'comparable':0,**request.get('request_coverage',{})}}
        if result['status']!='completed':
            status='submission_unknown' if result['status'] in {'submitted','result_unknown'} else result['status']
            snapshot.update(note='问道未完成：'+status+'；不自动重试。',error_code=status,submission_status=status)
            if not result.get('reused'):
                self.store.research.hotel_extraction(event,result={'status':'not_run','snapshot':snapshot})
            return snapshot
        raw=result['text'];snapshot['raw_text']=raw
        snapshot['submission_status']='completed'
        from core.tool_result import AgentToolContext
        snapshot['source_record_id']=self.store.tool_artifacts.save(
            AgentToolContext(event.platform,event.scope_id,event.sender_id,event.event_key),'hotel_quotes',raw)
        previous,_=self.store.research.hotel_extraction(event)
        if previous:return previous.get('snapshot',{**snapshot,'note':'报价提取结果未知，未自动重复调用模型。'})
        if not self.client or len(raw)>24000:
            snapshot['extraction_status']='not_run'
            snapshot['note']='模型不可用或原文超过提取输入上限，保留原文但不确认报价。'
            self.store.research.hotel_extraction(event,result={'status':'not_run','snapshot':snapshot})
            return snapshot
        _,reserved=self.store.research.hotel_extraction(event,reserve=True)
        if not reserved:return {**snapshot,'note':'报价提取已执行或结果未知，未重复调用。'}
        try:
            with model_call_context('hotel_quote_extract',stage_id='hotel.quote_extract',prompt_version=EVIDENCE_VERSION):
                response=self.client.chat.completions.create(model=self.model,response_format={'type':'json_object'},max_completion_tokens=4096,
                    messages=[{'role':'system','content':skill_text('hotel.quote_extract','system')},
                              {'role':'user','content':complete_json({'source':raw,'targets':{pid:{k:p.get(k) for k in ('name','address','cityname','adname','location')} for pid,p in targets.items()},'stay':public_stay(base['stay']),'city':spec['destination']},35000)}])
            extracted=response_json(response)
            snapshot.update(extracted_facts=extracted,evidence_version=EVIDENCE_VERSION)
            snapshot.update(**validate_quote_facts(extracted,raw,targets,base['stay'],spec['destination']),extraction_status='completed')
            snapshot['coverage'].update(returned=sum(normalized(p['name']) in normalized(raw) for p in targets.values()),bound=len({o['amap_poi_id'] for o in snapshot['offers'] if o['identity_status']=='bound'}),
                comparable=sum(o['status']=='reference_match' for o in evaluate(spec,snapshot,self.clock())))
        except ExecutionRevoked:raise
        except ModelBudgetExceeded as error:
            snapshot.update(note=str(error)+' 已保存本次问道原文，可本地读取。'+
                (' 如需重新核价，请明确回复“'+NEW_RUN_COMMAND+'”；新运行仍受日/月限额约束。' if error.code in RUN_LIMIT_CODES else ''),
                error_code=error.code,budget_block={'code':error.code,'details':error.details},extraction_status='budget_blocked')
        except Exception as error:snapshot.update(note='报价提取或证据校验未完成；保留原文，未再次调用问道。',error_type=type(error).__name__,extraction_status='failed')
        ensure_execution_active()
        self.store.research.hotel_extraction(event,result={'status':snapshot.get('extraction_status','failed'),'snapshot':snapshot})
        return snapshot
