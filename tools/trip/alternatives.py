"""Independent provider drafts, one comparison and the existing trip validator."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import hashlib
import json
import re
import uuid

from core.context_contract import complete_json, project_places, response_json
from core.execution_scope import ensure_execution_active, ExecutionRevoked
from core.model_budget import model_call_context
from infrastructure.skill_manager import skill_asset, skill_text
from tools.research.sources import provider_policy, day_sections
from tools.trip.planner import TripPlanError, bind_context, place_groups
from infrastructure.place_resolution import bind_visit_entities


RESEARCH_FIELDS={'research_context','research_candidates','activity_materials','planning_answers',
                 'planning_evidence','research_note','source_context','planning_decision','required_entities'}


class Alternatives:
    def __init__(self,store,planner,research):
        self.store,self.planner,self.research=store,planner,research

    def record(self,event,status,**details):
        self.store.diagnostics.record('planning',{'event_key':event.event_key,'status':status,**details})

    def checkpoint(self,event,key,action):
        saved=self.store.delivery.get_event_tool_result(key+':result')
        if saved:return json.loads(saved)
        nonce=uuid.uuid4().hex
        receipt=self.store.delivery.save_event_tool_result(key+':submitted',event.event_key,'trip_alternative',{},nonce)
        if receipt!=nonce:return {'status':'unknown'}
        try:
            result={'status':'completed',**action()}
        except ExecutionRevoked:
            raise
        except Exception as error:
            result={'status':'failed','error_type':type(error).__name__}
            if isinstance(error,TripPlanError):
                if getattr(error,'places',None):result['places']=error.places
                result.update(error_code=error.code,error_summary=str(error)[:400],
                    failure_stage=getattr(error,'failure_stage','plan_validation'),validation_path=error.path)
                candidate=getattr(error,'candidate',None)
                if candidate is not None and len(json.dumps(candidate,ensure_ascii=False))<=24000:
                    result['rejected_candidate']=candidate
        ensure_execution_active()
        self.store.delivery.save_event_tool_result(key+':result',event.event_key,'trip_alternative',{},json.dumps(result,ensure_ascii=False))
        return result

    def generate(self,event,spec,previous=None,request=''):
        from tools.research.tool import attach_answer
        from tools.research.evidence import planning_source
        base={k:deepcopy(v) for k,v in spec.items() if k not in RESEARCH_FIELDS}
        if base.get('city_windows') and base.get('start_date'):
            base['intercity_windows']=self.planner.transport_windows(base,{})
        reference_context=spec.get('source_context')
        key=hashlib.sha256(complete_json([event.event_key,base,previous,request,
            skill_asset('trip.compare').bundle_hash,skill_asset('trip.plan').bundle_hash],100000).encode()).hexdigest()
        def amap():
            local=deepcopy(base)
            collection=self.checkpoint(event,key+':amap-places',lambda:{'places':self.planner.collect_places(local)})
            if collection['status']!='completed':raise TripPlanError('地点资料未完整取得。',code='place_collection_failed',path='places')
            places=collection['places']
            plan=self.planner.plan(local,places,previous,request,attempts=1)
            return {'places':places,'plan':plan}
        def wendao():
            data={'query':event.content,'sources':[],'destination':base['destination'],
                  'provider_policy':provider_policy(event.content,automatic=True),
                  'provider_dates':{k:base[k] for k in ('start_date','end_date','day_count','date_is_tentative') if k in base},
                  'planning_requirements':deepcopy(base)}
            attach_answer(self.research,event,data,planning=True,timeout=75)
            if data.get('wendao_call',{}).get('status')!='completed':raise TripPlanError('问道草案未完成。')
            text='\n'.join(s['text'] for s in data['sources'] if s.get('provider')=='ctrip_wendao')
            _,days=day_sections(text)
            periods=list(re.finditer(r'(?m)^\s*(?:#{1,6}\s*)?(?:\*\*)?(上午|下午|晚上)(?:\*\*)?(?=[（(:：\s])',text))
            from tools.research.sources import sections
            end=next((s['heading_start'] for s in sections(text) if periods and s['heading_start']>periods[0].start()
                      and s['name'] in {t['name'] for t in data.get('provider_targets',[])}),len(text))
            schedule=text[:end] if periods else text
            data.update(report=text,brief_report=text)
            self.store.research.save(event,'partial',data)
            return {'data':data,'plan_available':bool(days or periods),'schedule_text':schedule,
                    'days_grounded':set(range(1,base['day_count']+1)) <= {row[0] for row in days}}
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='trip-draft') as pool:
            jobs={name:pool.submit(copy_context().run,self.checkpoint,event,key+':'+name,action)
                  for name,action in [('amap',amap),('wendao',wendao)]}
            results={name:job.result() for name,job in jobs.items()}
        a,b=results['amap'],results['wendao']
        for name,row in results.items():self.record(event,name+'_plan_'+('partial' if name=='wendao' and row.get('data') and not row.get('days_grounded') else row['status']),error_type=row.get('error_type',''))
        collection=json.loads(self.store.delivery.get_event_tool_result(key+':amap-places:result') or '{}')
        places=deepcopy(a.get('places') or collection.get('places',{}));data=b.get('data')
        if (a['status']!='completed' and not b.get('plan_available') and places and a.get('rejected_candidate')
                and a.get('error_code') in {'missing_required_entity','invalid_period','day_count','unexpected_poi','transport_conflict'}):
            repaired=self.checkpoint(event,key+':amap-repair',lambda:{'places':places,'plan':self.planner.plan(
                deepcopy(base),places,previous,request,attempts=1,repair_feedback=a)})
            if repaired['status']=='completed':a=repaired
        bindings={}
        if data:
            for target in data.get('provider_targets',[]):
                match=bind_visit_entities([target['name']],places,base['destination'])[target['name']]
                if match['status']=='not_found':
                    try:
                        rows=self.planner.amap.search_places(base['destination'],target['name'],offset=3)
                        for p in rows:
                            if p.get('id') and p.get('name'):places.setdefault(p['id'],p)
                        match=bind_visit_entities([target['name']],places,base['destination'])[target['name']]
                    except Exception as error:
                        if isinstance(error,ExecutionRevoked):raise
                        continue
                if match['status']=='bound':bindings[target['name']]=match['poi_id']
        for pid,group in place_groups(places).items():places[pid]['visit_group_id']=group
        prepared=deepcopy(base)
        required=bind_visit_entities(base.get('must_visit',[]),places,base['destination'])
        prepared['required_entities']=required
        unresolved=[name for name,row in required.items() if row['status']!='bound']
        if unresolved:
            self.record(event,'plan_compare_failed',error_code='required_entity_unresolved',phase='entity_binding')
            error=TripPlanError('尚未唯一确认必去地点：'+'、'.join(unresolved)+'。已有资料保留，请补充城市、园区或地址。',
                code='required_entity_unresolved',path='must_visit')
            error.places=places;error.spec=prepared;raise error
        if data:
            prepared['research_context']=deepcopy(planning_source(data).data)
            bind_context(prepared,places)
        considered={x['poi_id'] for day in a.get('plan',{}).get('days',[]) for item in day['activities']
                    for x in [item,*item.get('subactivities',[])]} | set(bindings.values())
        considered.update(a.get('plan',{}).get('hotel_ids',[]))
        considered.update(a.get('plan',{}).get('restaurant_ids',[]))
        considered.update(row['poi_id'] for row in required.values())
        def compare(repair=None):
            payload={'requirements':{**base,'required_entities':required},'amap_plan':a.get('plan'),
                'wendao_plan':{'text':b.get('schedule_text',''), 'days_grounded':b.get('days_grounded',False),
                               'bound_names':bindings} if data else None,
                'places':project_places(p for pid,p in places.items() if pid in considered)}
            if repair:payload['repair']={k:repair[k] for k in ('error_code','error_summary','validation_path','rejected_candidate') if k in repair}
            with model_call_context('trip_plan_compare',stage_id='trip.compare',repair_attempt=int(bool(repair))):
                response=self.planner.client.chat.completions.create(model=self.planner.model,response_format={'type':'json_object'},
                    messages=[{'role':'system','content':skill_text('trip.compare','system')},
                              {'role':'user','content':complete_json(payload,45000)}],timeout=60)
            value=response_json(response)
            try:
                if value.get('choice') not in {'amap','wendao','merge'}:raise TripPlanError('方案选择结构无效。',code='invalid_choice',path='choice')
                candidate=a.get('plan') if value['choice']=='amap' else value.get('plan')
                if candidate is None:raise TripPlanError('所选分支无可用方案。',code='missing_candidate',path='plan')
                allowed=considered
                if any(x.get('poi_id') not in allowed for day in candidate.get('days',[]) for x in day.get('activities',[])):
                    raise TripPlanError('融合增加了两版草案之外的景点。',code='unexpected_poi',path='plan.days')
                if value['choice']=='wendao' and any(x.get('poi_id') not in bindings.values() for day in candidate.get('days',[]) for x in day.get('activities',[])):
                    raise TripPlanError('问道方案包含未对应的地点。',code='unbound_poi',path='plan.days')
                return {'choice':value['choice'],'reason':str(value.get('reason',''))[:400],
                        'plan':self.planner.validate_plan(candidate,prepared,places)}
            except TripPlanError as error:
                error.candidate=value
                error.failure_stage='choice' if error.path=='choice' else 'candidate' if error.path=='plan' else 'plan_validation'
                raise
        decision=self.checkpoint(event,key+':compare',compare) if b.get('plan_available') else {
            'status':'skipped',**{k:a[k] for k in ('error_code','error_summary','validation_path','failure_stage') if k in a}}
        if decision.get('rejected_candidate') and decision.get('error_code') in {
                'invalid_choice','missing_candidate','unexpected_poi','unbound_poi','day_count','invalid_period','duplicate_visit_group','transport_conflict','missing_required_entity'}:
            repaired=self.checkpoint(event,key+':compare-repair',lambda:compare(decision))
            if repaired['status']=='completed':decision={**repaired,'repaired':True}
        if decision['status']!='completed':
            self.record(event,'plan_compare_failed',error_type=decision.get('error_type',''),error_code=decision.get('error_code',''),
                phase=decision.get('failure_stage','comparison'),validation={'code':decision.get('error_code',''),'path':decision.get('validation_path','')})
            if a['status']!='completed':
                error=TripPlanError('参考资料已保留，但尚无可确认草案：'+decision.get('error_summary',a.get('error_summary','规划校验未完成')),
                    code=decision.get('error_code',a.get('error_code','invalid_plan')),path=decision.get('validation_path',a.get('validation_path','')))
                error.places=places;error.spec=prepared;error.failure_stage=decision.get('failure_stage','plan_validation');raise error
            decision={**decision,'choice':'amap','plan':self.planner.validate_plan(a['plan'],prepared,places),
                      'reason':'比较未完成，采用已通过校验的高德草案。'}
        self.record(event,'plan_compare_'+decision['status'],decision_source=decision['choice'],error_type=decision.get('error_type',''),
                    error_code=decision.get('error_code',''),phase=decision.get('failure_stage',''),
                    validation={'code':decision.get('error_code',''),'path':decision.get('validation_path','')})
        prepared['planning_decision']={k:v for k,v in decision.items() if k not in {'plan','rejected_candidate'}}
        prepared['planning_decision']['source_event_id']=event.event_key
        if reference_context is not None:prepared['source_context']=reference_context
        prepared['research_note']=('已获取携程问道参考；'+(('两版方案已完成比较。' if decision['status']=='completed' else '比较结果未通过，采用已通过校验的高德草案。') if b.get('plan_available') else '本次资料未形成完整逐日草案，采用高德方案。')
            if data else '问道本次未取得完整回答，采用已通过校验的高德方案。')
        spec.clear();spec.update(prepared)
        return places,decision['plan']
