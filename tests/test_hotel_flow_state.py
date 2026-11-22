"""S1/S2: stable candidate state, canonical history and local owned actions."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import json
import unittest
from unittest.mock import Mock

import test_universal_hotel_prices as fixtures
from core.hotel_contract import date_proposal,merge_hotel_state
from core.intent_ir import IntentIR,IntentOperation
from core.chat_transport import ChatEvent
from core.context_contract import hotel_state_projection,task_projection,complete_json
from tools.hotel_prices.quotes import lodging_target,target_key,target_matches
from tools.hotel_prices.tool import HotelQuotes
from app.request_router import RequestRouter


class HotelFlowStateTests(unittest.TestCase):
    setUp=fixtures.UniversalHotelTests.setUp
    event=fixtures.UniversalHotelTests.event
    call=fixtures.UniversalHotelTests.call

    def test_night_duration_measure_words_share_checkout_normalization(self):
        from core.hotel_contract import resolve_stay
        for text,checkout in [('住一个晚上','2026-11-13'),('住一晚','2026-11-13'),('住两夜','2026-11-14'),('住2个晚上','2026-11-14')]:
            result=resolve_stay({'check_in':'十一月十二日'},'2026-11-08T09:00:00+08:00',text)
            self.assertEqual(result,{'check_in':'2026-11-12','check_out':checkout})
        result=resolve_stay({'check_in':'2026-11-12','check_out':'2026-11-13'},'2026-11-08T09:00:00+08:00',
            '再住两个晚上',previous={'check_out':'2026-11-13'})
        self.assertEqual(result['check_out'],'2026-11-15')

    def turn(self,text,values):
        event=self.event(text);claim=self.store.delivery.begin_event(event.event_key)
        compiler=Mock();compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',values),),source_spans=(text,))
        reply,_=RequestRouter(self.store,compiler,read_only_service=self.owner).route(event,claim)
        return reply,self.store.tasks.for_event(event,event.event_key),event,compiler

    def menu(self):
        self.map.search_places.return_value=[*fixtures.TARGET.values(),{**fixtures.TARGET['h'],'id':'north','name':'云泽酒店(北门店)','address':'北路1号'},
            {'id':'parking','name':'云泽酒店停车场','typecode':'150904','type':'交通设施服务;停车场','cityname':'测试城','address':'南路1号'}]
        request={**deepcopy(fixtures.REQUEST),'targets':[{'name':'云泽','city':'测试城'}]}
        return self.turn(fixtures.FULL.replace(fixtures.NAME,'云泽'),request)

    def test_admission_uses_category_and_rechecks_legacy_selection(self):
        for place,expected in (({'typecode':'100105','type':'住宿服务;宾馆酒店'},True),
            ({'typecode':'150904','type':'交通设施服务;停车场','name':'酒店停车场'},False),
            ({'typecode':'100105','type':'交通设施服务;停车场'},False),({'name':'某酒店'},False),
            ({'type':'住宿服务;旅馆招待所'},True)):
            with self.subTest(place=place):self.assertEqual(lodging_target(place),expected)
        _,task,_,_=self.menu()
        self.assertEqual(len(task.slots['hotel_candidates']),2)
        bad={**fixtures.TARGET['h'],'typecode':'150904','type':'交通设施服务;停车场'}
        result=self.hotel.lookup(self.event(),{'city':'测试城','stay':fixtures.STAY},targets={'bad':bad},policy={'requested':True})
        self.assertEqual(result['error_code'],'invalid_lodging_target');self.transport.assert_not_called()

    def test_bare_number_is_local_and_invalid_then_valid_preserves_identity(self):
        _,first,_,_=self.menu();identity=first.slots['candidate_set_id']
        from app.request_router import bind_request
        self.assertEqual(bind_request(self.store,self.event('一')).budget_run_key,first.slots['budget_run_key'])
        _,invalid,_,_=self.turn('9',{})
        self.assertEqual(invalid.slots['hotel_candidates'],first.slots['hotel_candidates'])
        self.assertEqual(invalid.slots['candidate_set_id'],identity);self.transport.assert_not_called()
        reply,selected,_,compiler=self.turn('一',{})
        compiler.try_compile.assert_not_called()
        self.assertEqual(selected.status,'completed',reply);self.assertEqual(self.transport.call_count,1)
        self.assertEqual(self.map.search_places.call_count,1)
        self.assertEqual(selected.slots['selected_candidate_ids'],['h'])

    def test_numbers_do_not_choose_between_tasks_or_while_collecting_occupancy(self):
        _,task,event,_=self.menu()
        other=replace(task,task_id='other')
        self.assertIsInstance(self.hotel.local_selection(self.event('1'),(task,other)),str)
        collecting=replace(task,missing_slots=('adults',))
        self.assertIsNone(self.hotel.local_selection(self.event('1'),(collecting,)))
        self.transport.assert_not_called()

    def test_switching_choice_does_not_keep_the_previous_selected_hotel(self):
        self.menu();self.turn('一',{})
        _,selected,_,_=self.turn('选择第二家',{'reference':{'ordinal':2,'selection':'第二家'}})
        self.assertEqual(set(selected.slots['hotel_targets']),{'north'})
        query=json.loads(self.vendor_query())
        self.assertEqual([p['target_id'] for p in query['hotels']],['north'])
        self.assertEqual(self.map.search_places.call_count,1)

    def vendor_query(self):
        query=self.transport.call_args.kwargs['payload']['query']
        return query[query.index('{'):]

    def test_changed_range_empty_search_replaces_version_without_old_selection(self):
        _,task,_,_=self.menu();self.map.search_places.return_value=[]
        reply,updated,_,_=self.turn('改找另外城的酒店',{'discovery':{'city':'另外城'}})
        self.assertEqual(updated.slots['selection_status'],'empty',reply)
        self.assertEqual(updated.slots['hotel_candidates'],[])
        self.assertGreater(updated.slots['candidate_set_version'],task.slots['candidate_set_version'])
        self.assertNotIn('请选择具体分店：',reply);self.transport.assert_not_called()

    def test_partial_binding_and_aliases_share_one_target(self):
        original=fixtures.TARGET['h'];north={**original,'id':'north','name':'云泽酒店(北门店)','address':'北路1号'}
        self.map.search_places.side_effect=lambda city,name:[original] if name==fixtures.NAME else [north,{**north,'id':'other','name':'云泽酒店(南门店)'}]
        request={**deepcopy(fixtures.REQUEST),'targets':[*fixtures.REQUEST['targets'],{'name':'北边云泽','city':'测试城'}]}
        _,task,_,_=self.turn(fixtures.FULL+'，还有测试城北边云泽',request)
        self.assertIn('h',task.slots['hotel_targets'])
        _,done,_,_=self.turn('选择第一家',{'reference':{'ordinal':1,'selection':'第一家'}})
        self.assertEqual(set(done.slots['hotel_targets']),{'h','north'});self.assertEqual(self.map.search_places.call_count,2)
        self.assertEqual(self.transport.call_count,1)

    def test_duplicate_names_and_alias_same_poi_do_not_produce_empty_menu(self):
        self.map.search_places.return_value=list(fixtures.TARGET.values())
        request={**deepcopy(fixtures.REQUEST),'targets':[*fixtures.REQUEST['targets'],{'name':'湖畔云泽','city':'测试城'}]}
        result=self.call(request,self.event(fixtures.FULL+'，湖畔云泽'))
        self.assertEqual(result.status,'completed',result.data);self.assertEqual(self.transport.call_count,1)
        self.assertEqual(result.payload['snapshot']['coverage']['unique_hotels'],1)

    def test_duplicate_input_coverage_and_mapping_survive_source_storage(self):
        request=deepcopy(fixtures.REQUEST);request['targets']*=2
        event=self.event();result=self.call(request,event)
        self.assertEqual(result.payload['request_coverage']['input_items'],2)
        saved=self.hotel.history(event,{})[0]
        self.assertEqual(saved['coverage']['input_items'],2);self.assertEqual(saved['coverage']['unique_requested'],1)
        self.assertEqual([row['poi_id'] for row in saved['input_mapping']],['h','h'])

    def test_address_scope_is_not_lost_in_deduplication_or_selection(self):
        target={'name':'同名酒店','city':'测试城','address_hint':'8-1号'}
        self.assertNotEqual(target_key(target),target_key({**target,'address_hint':'81号'}))
        self.assertFalse(target_matches({'type':'住宿服务;酒店','cityname':'测试城','address':'81号'},target))
        self.assertEqual(target_key({'name':'酒店','city':'南京'}),target_key({'name':'酒店','city':'南京市'}))

    def test_receipt_merge_preserves_absent_fields_and_honors_explicit_empty(self):
        old={'hotel_candidates':[{'id':'x'}],'hotel_request':{'stay':fixtures.STAY}}
        unchanged=merge_hotel_state(old,{'selection_status':'invalid','selected_candidate_ids':[]})
        self.assertEqual(unchanged['hotel_candidates'],old['hotel_candidates'])
        self.assertEqual(merge_hotel_state(unchanged,{'target_candidates':[]})['hotel_candidates'],[])

    def test_legacy_lost_list_can_be_restored_from_an_owned_quoted_receipt(self):
        _,task,source,_=self.menu()
        receipt=json.loads(self.store.delivery.get_event_tool_result(self.owner._key(source,'semantic')+':0'))
        broken={**task.slots,'hotel_candidates':[]};broken.pop('candidate_set_id',None)
        event=self.event('选择第一家',message_reference={'event_id':source.event_key,'tool_results':[receipt]})
        values={'reference':{'selection':'第一家','ordinal':1}}
        restored=self.call(values,event,broken)
        self.assertEqual(restored.status,'needs_input');self.assertIn('恢复',restored.data);self.transport.assert_not_called()
        state=merge_hotel_state(broken,restored.payload)
        result=self.call(values,self.event('选择第一家'),state)
        self.assertEqual(result.status,'completed',result.data);self.assertEqual(self.transport.call_count,1)

    def test_history_filter_uses_only_current_fields_and_keeps_source_immutable(self):
        first=self.event();self.call(event=first)
        changed=deepcopy(fixtures.REQUEST);changed['stay'].update(check_in='2030-01-10',check_out='2030-01-11')
        self.call(changed,self.event(fixtures.FULL.replace('2030-01-07','2030-01-10').replace('2030-01-09','2030-01-11')))
        before=self.hotel.history(first,{})
        result=self.call({'mode':'saved','stay':{'check_in':'2030年1月7日'}},self.event('查看2030年1月7日入住那次原文'),{'hotel_request':changed})
        self.assertEqual(result.status,'completed',result.data);self.assertIn('2030-01-09',result.data)
        self.assertEqual(self.transport.call_count,2);self.assertEqual(self.hotel.history(first,{}),before)
        missing=self.call({'mode':'saved','stay':{'check_in':'2020年1月7日'}},self.event('查看2020年1月7日入住的旧资料'))
        self.assertIn('未命中',missing.data);self.assertIn('2030-01-07',missing.data)
        self.assertEqual(self.transport.call_count,2)

    def test_history_read_does_not_replace_a_pending_query(self):
        self.call();_,pending,_,_=self.menu()
        reply,_,event,_=self.turn('查看2030年1月7日入住那次原文',{'mode':'saved','stay':{'check_in':'2030年1月7日'}})
        self.assertIn('原文',reply)
        self.assertEqual(self.store.tasks.owned(event,pending.task_id),pending)

    def test_history_scans_beyond_cache_window_and_invalid_filter_does_not_fall_back(self):
        for i in range(21):
            event=self.event('合成历史记录'+str(i));day=datetime(2020,1,7,tzinfo=timezone.utc)+timedelta(days=i)
            raw='合成已保存原文 '+day.date().isoformat()
            self.transport.return_value=(raw.encode(),'','text/markdown')
            result=self.hotel.wendao.query(event,self.store.research,'合成历史资料',purpose='hotel_quotes',fingerprint=str(i))
            snapshot={'source_event_id':event.event_key,'raw_text':raw,'retrieved_at':result['retrieved_at'],'city':'测试城',
                'stay':{**fixtures.STAY,'check_in':day.date().isoformat(),'check_out':(day+timedelta(days=1)).date().isoformat()},
                'requested_hotels':{'h':fixtures.NAME},'targets':fixtures.TARGET,'offers':[]}
            self.store.research.hotel_extraction(event,result={'status':'completed','snapshot':snapshot})
        result=self.call({'mode':'saved','stay':{'check_in':'2020年1月7日'}},self.event('查看2020年1月7日入住的原文'))
        self.assertEqual(result.status,'completed',result.data);self.assertIn('合成已保存原文 2020-01-07',result.data)
        invalid=self.call({'mode':'saved','stay':{'check_in':'2020年2月31日'}},self.event('查看2020年2月31日入住的原文'),{'hotel_request':fixtures.REQUEST})
        self.assertEqual(invalid.error_code,'unresolved_hotel_date');self.assertEqual(self.transport.call_count,21)

    def test_proposal_response_is_semantic_and_stale_or_negative_cannot_adopt(self):
        state={'hotel_request':{'targets':fixtures.REQUEST['targets']},'hotel_date_proposal':date_proposal({k:fixtures.STAY[k] for k in ('check_in','check_out')})}
        text='好，就使用建议日期，一间房，两名成人，无儿童'
        result=self.call({'proposal_response':{'kind':'stay_dates','decision':'accept','source':'好，就使用建议日期'},
            'stay':{'rooms':1,'adults':2,'children_ages':[]}},self.event(text),state)
        self.assertEqual(result.status,'completed',result.data);self.assertEqual(self.transport.call_count,1)
        text='不是，改到2030-01-10入住'
        denied=self.call({'proposal_response':{'kind':'stay_dates','decision':'reject','source':'不是'},'stay':{'check_in':'2030-01-10'}},self.event(text),state)
        self.assertEqual(denied.status,'needs_input');self.assertNotIn('check_out',denied.payload['request']['stay'])
        expired={**state,'hotel_expired':True}
        stale=self.call({'proposal_response':{'kind':'stay_dates','decision':'accept','source':'同意'}},self.event('同意'),expired)
        self.assertEqual(stale.status,'needs_input');self.assertEqual(self.transport.call_count,1)
        unseen={**state,'hotel_date_proposal':{**state['hotel_date_proposal'],'presented':False}}
        result=self.call({'proposal_response':{'kind':'stay_dates','decision':'accept','source':'同意'}},self.event('同意'),unseen)
        self.assertEqual(result.status,'needs_input');self.assertEqual(self.transport.call_count,1)

    def test_all_hotel_model_projections_exclude_long_evidence_and_source_text(self):
        _,task,_,_=self.menu();huge='私有研究长文'*5000
        slots={**task.slots,'source_context':{'provider_policy':{'offline':True},'research_context':{'raw_text':huge}},
            'hotel_request':{**task.slots['hotel_request'],'source_spans':[huge],'stay':{**fixtures.STAY,'field_sources':{'adults':huge}}}}
        projected=task_projection(replace(task,slots=slots,initial_request=huge))
        raw=json.dumps(projected,ensure_ascii=False)
        self.assertLess(len(raw),5000);self.assertNotIn(huge,raw);self.assertTrue(projected['slots']['source_policy']['offline'])
        self.assertEqual(projected['slots']['hotel_request']['stay']['children_ages'],[])
        with self.assertRaises(ValueError) as error:complete_json({'context':huge},22000,purpose='intent')
        self.assertEqual(error.exception.details['path'],'intent');self.assertNotIn(huge,str(error.exception.details))


class HotelWebActionsTests(unittest.TestCase):
    def setUp(self):
        from test_web_app import WebTests
        from test_hotel_quotes import fixture,TARGET,STAY
        from test_travel_agent import FakeClient
        from test_trips import model_response
        from infrastructure.wendao_client import WendaoClient,ENDPOINT
        self.web=WebTests();self.web.setUp();self.addCleanup(self.web.doCleanups)
        raw,offer=fixture();self.vendor=Mock(return_value=(raw.encode(),ENDPOINT,'text/markdown'))
        self.map=Mock();self.map.search_places.return_value=[*TARGET.values(),{**TARGET['h'],'id':'other','name':'云泽酒店(北门店)'}]
        self.models=FakeClient([model_response({'offers':[offer]}) for _ in range(4)])
        self.hotel=HotelQuotes(self.web.store,WendaoClient('test',True,transport=self.vendor),self.models,'test',amap=self.map)
        self.web.runtime.travel_service.hotel_prices=self.hotel
        self.compiler=Mock();self.web.runtime.application.semantic_task_service=RequestRouter(self.web.store,self.compiler,read_only_service=self.web.runtime.travel_service)
        self.request={**deepcopy(fixtures.REQUEST),'targets':[{'name':'云泽','city':'测试城'}]}
        self.text=fixtures.FULL.replace(fixtures.NAME,'云泽')

    def seed(self):
        self.compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',self.request),),source_spans=(self.text,))
        self.web.send(self.text,key='menu');self.assertTrue(self.web.run_job())
        return self.web.messages()[-1]

    def post(self,action,key):
        data={k:v for k,v in action.items() if k not in {'label','selectable'}}
        return self.web.client.post(f'/api/conversations/{self.web.identity}/hotel-actions',headers=self.web.headers,json={**data,'client_request_id':key})

    def test_candidate_button_replay_and_stale_versions_have_one_submission(self):
        message=self.seed();action=message['hotel_actions'][0];self.compiler.reset_mock()
        from app.request_router import bind_request
        action_body={k:v for k,v in action.items() if k not in {'label','selectable'}}
        event=replace(self.web.event('选择当前酒店候选','check-budget'),hotel_action=action_body)
        bound=bind_request(self.web.store,event);task=self.web.store.tasks.owned(event,action['task_id'])
        self.assertEqual(bound.budget_run_key,task.slots['budget_run_key'])
        first=self.post(action,'choose');second=self.post(action,'choose')
        self.assertEqual(first.status_code,202,first.text);self.assertEqual(first.json()['job_id'],second.json()['job_id'])
        self.assertTrue(self.web.run_job());self.assertEqual(self.post(action,'choose').status_code,202)
        self.assertEqual(self.post(action,'old-version').status_code,409)
        self.compiler.try_compile.assert_not_called();self.assertEqual(self.vendor.call_count,1)
        self.assertEqual(len(self.models.completions.requests),1)
        self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))

    def test_original_get_and_bound_text_do_not_need_models_or_configuration(self):
        action=self.seed()['hotel_actions'][0];self.post(action,'select');self.web.run_job()
        original=next(a for a in self.web.messages()[-1]['hotel_actions'] if a['kind']=='view_original')
        self.hotel.enabled=False;self.hotel.client=None;self.web.runtime.application.travel_agent=None
        self.compiler.try_compile.side_effect=AssertionError('Local original must bypass compiler')
        url=f"/api/conversations/{self.web.identity}/hotel-quotes/{original['source_event_id']}/original?revision={original['revision']}"
        result=self.web.client.get(url);self.assertEqual(result.status_code,200,result.text)
        self.assertIn('399',result.json()['text']);self.assertTrue(result.json()['historical'])
        self.web.send('查看酒店报价原文',key='read-local');self.assertTrue(self.web.run_job())
        self.assertIn('原文已读完',self.web.messages()[-1]['content']);self.assertEqual(self.vendor.call_count,1)
        other=self.web.client.post('/api/conversations',headers=self.web.headers).json()['id']
        foreign=self.web.client.get(url.replace(self.web.identity,other));self.assertEqual(foreign.status_code,404)

    def test_original_pages_keep_all_characters_and_revision(self):
        action=self.seed()['hotel_actions'][0];self.post(action,'select');self.web.run_job()
        source=self.web.messages()[-1]['hotel_actions'][0]['source_event_id'];event=self.web.event('')
        quote=self.web.store.research.hotel_quote_for_event(event,source)
        raw=quote['raw_text'];parts=[];cursor=0
        while True:
            page=self.hotel.original(event,source,cursor=cursor,max_chars=80);parts.append(page['text'])
            if page['next_cursor'] is None:break
            cursor=page['next_cursor']
        self.assertEqual(''.join(parts),raw)
        with self.assertRaises(ValueError):self.hotel.original(event,source,'wrong-revision')

    def test_candidate_actions_cannot_cross_conversation(self):
        action=self.seed()['hotel_actions'][0]
        other=self.web.client.post('/api/conversations',headers=self.web.headers).json()['id']
        data={k:v for k,v in action.items() if k not in {'label','selectable'}}
        response=self.web.client.post(f'/api/conversations/{other}/hotel-actions',headers=self.web.headers,json={**data,'client_request_id':'foreign'})
        self.assertEqual(response.status_code,409);self.vendor.assert_not_called()

    def test_date_buttons_bind_the_current_proposal_and_bypass_compiler(self):
        # Seed a displayed proposal in the isolated task, independently of itinerary/model creation.
        self.request=deepcopy(fixtures.REQUEST);self.request['stay']={k:fixtures.STAY[k] for k in ('rooms','adults','children_ages')}
        self.text=fixtures.FULL
        self.seed();event=self.web.event('');task=self.web.store.tasks.recent('web',event.storage_scope_id,event.sender_id)[0]
        state={**task.slots,'hotel_date_proposal':date_proposal({k:fixtures.STAY[k] for k in ('check_in','check_out')})}
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(state),task.task_id))
        action=next(a for a in self.web.messages()[-1]['hotel_actions'] if a['kind']=='accept_dates')
        self.compiler.reset_mock();response=self.post(action,'dates')
        self.assertEqual(response.status_code,202,response.text);self.assertTrue(self.web.run_job())
        self.compiler.try_compile.assert_not_called();self.assertEqual(self.vendor.call_count,1)
        self.assertEqual(self.post(action,'stale-dates').status_code,409)

    def test_history_index_exposes_local_buttons_and_does_not_repurpose_bare_numbers(self):
        first=self.seed();self.post(first['hotel_actions'][0],'choose');self.web.run_job()
        raw=self.hotel.history(self.web.event(''),{})[0]['raw_text']
        # A second owned source with different dates creates an unambiguous local index action.
        event=self.web.event('synthetic history','history-two')
        response=self.hotel.wendao.query(event,self.web.store.research,'合成历史资料',purpose='hotel_quotes',fingerprint='second')
        snapshot={'source_event_id':event.event_key,'raw_text':raw+'\n第二次合成资料','city':'测试城','retrieved_at':response['retrieved_at'],
            'stay':{**fixtures.STAY,'check_in':'2030-02-01','check_out':'2030-02-03'},'requested_hotels':{'h':fixtures.NAME},'targets':fixtures.TARGET,'offers':[]}
        self.web.store.research.hotel_extraction(event,result={'status':'completed','snapshot':snapshot})
        self.compiler.reset_mock();self.web.send('查看酒店报价记录',key='history-list');self.assertTrue(self.web.run_job())
        message=self.web.messages()[-1];actions=message['hotel_actions']
        self.assertEqual(len(actions),2);self.assertTrue(all(a['kind']=='view_original' for a in actions))
        self.compiler.try_compile.assert_not_called();self.assertEqual(self.vendor.call_count,2)
        self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,
            json={'content':'查看酒店报价记录第1条原文','client_request_id':'history-one','quoted_message_id':message['id']})
        self.assertTrue(self.web.run_job());self.assertIn('已保存的问道原文',self.web.messages()[-1]['content'])
        self.assertEqual(self.vendor.call_count,2)
