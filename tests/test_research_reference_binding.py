from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.chat_transport import ChatEvent
from core.intent_ir import IntentIR, IntentOperation
from core.research_contract import project_request
from infrastructure.memory_store import MemoryStore
from infrastructure.execution_report import explain
from infrastructure.wendao_client import WendaoClient, ENDPOINT
from tools.research.tool import (ResearchTool, resolve_plan, structured_target_scope,
    attach_answer, answer_report, write_answer)

NAMES = ['六朝博物馆','总统府','江宁织造博物馆','古鸡鸣寺','南京城墙景区(神策门至太平门)',
         '玄武湖景区','南京博物院','白马公园','九华山公园','南京地质博物馆','北极阁公园','环洲']
MESSAGE = '那再为我分别介绍一下这些景点信息'


class ResearchReferenceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name)/'test.db')
        self.event = ChatEvent('web','group','manual','c','owner',MESSAGE,
                              occurred_at=datetime(2026,10,24,tzinfo=timezone.utc))
        self.trip = {'trip_id':'TR-current','title':'南京4日行程','version':2,'status':'active',
            'spec':{'destination':'南京','day_count':4,'start_date':'2026-10-22','end_date':'2026-10-25',
                    'date_is_tentative':True,'date_reference_day':'2026-10-21'},
            'sources':{str(i):{'name':name,'address':'南京市测试路'} for i,name in enumerate(NAMES)},
            'plan':{'days':[{'day_index':d+1,'date':f'2026-10-{22+d}',
                'activities':[{'poi_id':str(i),'period':('上午','下午','晚上')[i%3],
                    'reason':'文化参观'} for i in range(d*3,(d+1)*3)]} for d in range(4)]}}
        self.artifact = {'ordinal':1,'title':'南京参考方案（2天）','destination':'南京','version':1,
            'source_event_id':'old','duration_options':[2],'content':'第1天：六朝博物馆；第2天：玄武湖景区'}
        self.store.planning.list_for_owner = Mock(return_value=(self.artifact,))
        self.store.trips.list_for_owner = Mock(return_value=(self.trip,))
        self.store.trips.planning_dates_for_event = Mock(return_value={**self.trip['spec'],'day_count':2})

    def request(self):
        values,_ = project_request({'query_text':MESSAGE},MESSAGE)
        return {**values,'original_request':MESSAGE}

    def data(self):
        plan,_ = resolve_plan(self.store,self.event,self.request())
        return {'query':MESSAGE,'sources':[],
                'target_scope':{**structured_target_scope(self.request(),plan),'city':'南京'},
                'provider_dates':{'start_date':'2026-10-22','day_count':4,'date_is_tentative':True}}

    def service(self, text):
        transport = Mock(return_value=(text.encode(),ENDPOINT,'text/markdown'))
        provider = WendaoClient('offline-fixture',True,transport=transport)
        registry = Mock(entries=())
        service = ResearchTool(self.store,Mock(configured=True),amap=Mock(),official_sources=registry,wendao=provider)
        return service,transport

    def test_missing_reference_is_recovered_from_bare_followup(self):
        self.assertEqual(self.request()['plan_reference'],'这些景点')

    def test_reordered_generic_followups_keep_the_committed_scope_without_extraction(self):
        from core.research_contract import infer_plan_reference
        for text in ('这些景点的信息再为我介绍一下','再为我介绍一下这些景点的信息','请查看这些景点的开放时间'):
            self.assertEqual(infer_plan_reference(text),'这些景点')
        for text in ('介绍这些景点中的前三个','这些景点和武汉的酒店分别介绍一下'):
            self.assertEqual(infer_plan_reference(text),'')

    def test_explicit_new_city_is_not_automatically_bound_to_old_trip(self):
        text='请介绍杭州这些景点：西湖、灵隐寺'
        values,_=project_request({'query_text':text},text)
        self.assertNotIn('plan_reference',values)

    def test_generic_reference_uses_four_day_trip_while_old_artifact_is_active(self):
        plan,reason=resolve_plan(self.store,self.event,self.request())
        self.assertEqual(reason,'saved_trip')
        self.assertEqual(plan['version'],2)
        self.assertEqual(plan['duration_options'],[4])
        self.assertEqual(len(structured_target_scope(self.request(),plan)['targets']),12)

    def test_context_inferred_artifact_index_does_not_override_current_trip_pointer(self):
        for reference in (None,'这些景点信息','景点'):
            with self.subTest(reference=reference):
                raw={'query_text':MESSAGE,'plan_index':1}
                if reference is not None:
                    raw['plan_reference']=reference
                values,_=project_request(raw,MESSAGE)
                plan,reason=resolve_plan(self.store,self.event,values)
                self.assertEqual(reason,'saved_trip')
                self.assertEqual(plan['version'],2)

    def test_explicit_historical_reference_still_uses_original_artifact(self):
        plan,reason=resolve_plan(self.store,self.event,{'plan_reference':'最开始的方案','plan_index':1})
        self.assertEqual(reason,'explicit_reference')
        self.assertEqual(plan['duration_options'],[2])

    def test_multiple_current_trips_clarify_instead_of_using_unique_old_artifact(self):
        self.store.trips.list_for_owner.return_value=(self.trip,{**self.trip,'trip_id':'TR-other'})
        self.assertEqual(resolve_plan(self.store,self.event,self.request()),(None,'ambiguous_plan'))

    def test_subset_retains_semantic_extractor(self):
        request={**self.request(),'original_request':'只介绍第三天这些景点'}
        plan,_=resolve_plan(self.store,self.event,self.request())
        self.assertIsNone(structured_target_scope(request,plan))

    def test_named_subset_retains_semantic_extractor(self):
        request={**self.request(),'original_request':'介绍这些景点里的南京博物院'}
        plan,_=resolve_plan(self.store,self.event,self.request())
        self.assertIsNone(structured_target_scope(request,plan))

    def test_all_twelve_targets_are_sent_in_one_bounded_provider_request(self):
        text='\n\n'.join('### '+name+'\n城市：南京市\n- 开放时间：09:00-17:00\n- 普通票价：免费'
                           for name in NAMES)
        service,transport=self.service(text)
        data=self.data()
        attach_answer(service,self.event,data,targets=data['target_scope']['targets'],dates={})
        query=transport.call_args.kwargs['payload']['query']
        self.assertEqual(transport.call_count,1)
        self.assertIn('研究城市：南京',query)
        self.assertIn('[target-12]',query)
        self.assertEqual(data['provider_scope_validation']['matched_count'],12)

    def test_wrong_city_is_rejected_before_materials_or_writing_and_is_not_retried(self):
        service,transport=self.service('### 外滩\n位于上海市。\n- 开放时间：全天\n- 普通票价：免费')
        service.search_planner.plan_targets=Mock(side_effect=AssertionError('should use committed POIs'))
        service.client=Mock()
        ir=IntentIR(1,'read',(IntentOperation('research','search_travel_guides',{'query_text':MESSAGE}),),
                    source_spans=(MESSAGE,))
        reply=service.apply_intent(self.event,None,ir)
        self.assertIn('已拦截',reply)
        self.assertNotIn('外滩',reply)
        saved=self.store.research.get(self.event)
        self.assertTrue(saved['scope_rejected'])
        self.assertEqual(saved['sources'],[])
        self.assertEqual(answer_report(saved),reply)
        self.assertEqual(write_answer(service,self.event,saved),reply)
        self.assertEqual(service.apply_intent(self.event,None,ir),reply)
        self.assertEqual(transport.call_count,1)
        service.client.chat.completions.create.assert_not_called()
        service.search_planner.plan_targets.assert_not_called()

    def test_partial_reply_keeps_only_bound_target_materials(self):
        service,_=self.service('### 六朝博物馆\n城市：南京市\n六朝文化。\n- 开放时间：09:00-17:00\n'
                              '### 外滩\n位于上海市。\n- 开放时间：全天')
        data=self.data()
        attach_answer(service,self.event,data,targets=data['target_scope']['targets'],dates={})
        self.assertEqual(data['provider_scope_validation']['matched_count'],1)
        self.assertTrue(all(r['name']=='六朝博物馆' for r in data['activity_materials']))

    def test_inflight_research_does_not_mix_in_a_new_trip_version(self):
        service,transport=self.service('unused')
        plan,_=resolve_plan(self.store,self.event,self.request())
        self.store.research.save(self.event,'searching',{
            'query':MESSAGE,'sources':[],
            'target_scope':structured_target_scope(self.request(),plan),
            'plan_snapshot':plan,'plan_version':2})
        self.trip['version']=3
        current,_=resolve_plan(self.store,self.event,self.request())
        reply=service._plan_facts(self.event,{**self.request(),'plan':current})
        self.assertIn('版本已变化',reply)
        transport.assert_not_called()

    def test_scope_rejection_is_visible_as_a_business_failure_in_diagnostics(self):
        row=explain({'id':1,'stage':'routing','created_at':'2026-10-24',
            'details':{'status':'research_scope_rejected','reason':'provider_scope_mismatch'}})
        self.assertTrue(row['warning'])
        self.assertIn('已拦截无关内容',row['description'])
        self.assertNotIn('规则未完整覆盖',row['description'])

    def test_rewording_does_not_relabel_old_material_as_current_trip(self):
        service,transport=self.service('unused')
        current,_=resolve_plan(self.store,self.event,self.request())
        saved={**self.data(),'stage':'partial','report':'old',
               'sources':[{'provider':'ctrip_wendao'}],
               'plan_snapshot':{**current,'version':1}}
        self.store.research.latest=Mock(return_value=[saved])
        event=ChatEvent('web','group','rewrite','c','owner','把这些景点介绍得更丰富')
        with patch('tools.research.tool.write_answer') as writer:
            reply=service._saved_answer_request(event,plan=current)
        self.assertIn('未绑定当前行程版本',reply)
        writer.assert_not_called()
        transport.assert_not_called()

    def test_rewording_can_reuse_material_bound_to_the_same_trip_version(self):
        service,transport=self.service('unused')
        current,_=resolve_plan(self.store,self.event,self.request())
        saved={**self.data(),'stage':'partial','report':'saved',
               'sources':[{'provider':'ctrip_wendao'}],'plan_snapshot':current}
        self.store.research.latest=Mock(return_value=[saved])
        event=ChatEvent('web','group','rewrite','c','owner','把这些景点介绍得更丰富')
        with patch('tools.research.tool.write_answer',return_value='rewritten') as writer:
            self.assertEqual(service._saved_answer_request(event,plan=current),'rewritten')
        writer.assert_called_once()
        transport.assert_not_called()


if __name__=='__main__':
    unittest.main()
