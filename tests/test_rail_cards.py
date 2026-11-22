import json
from datetime import date,datetime,timezone
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import test_web_app as fixtures
from app.request_router import RequestRouter
from core.chat_transport import OutgoingMessage
from core.intent_ir import IntentIR,IntentOperation
from core.tool_result import ToolResult
from infrastructure.rail_client import RailClient
from tools.travel_query import TravelQueryTool,rail_parameters


FIXTURE=json.loads((Path(__file__).parent/'fixtures/rail_city_pagination_2026-11-01.json').read_text(encoding='utf-8'))


class RailCardTests(unittest.TestCase):
    def setUp(self):
        self.web=fixtures.WebTests(); self.web.setUp(); self.addCleanup(self.web.doCleanups)
        fixed=datetime(2026,10,31,8,tzinfo=timezone.utc)
        for module in ('tools.travel_query','app.request_router','infrastructure.web_repository'):
            clock=patch(module+'.datetime',wraps=datetime); mock=clock.start();self.addCleanup(clock.stop);mock.now.return_value=fixed
        self.calls=[]
        def execute(name,arguments,context):
            self.calls.append(arguments)
            from datetime import datetime
            request,missing=rail_parameters(arguments,datetime.fromisoformat(context.execution_data['reference_time']))
            self.assertFalse(missing)
            request['departure_window']=list(request['departure_window'])
            trains=RailClient.normalize_tickets(FIXTURE['trains'],date.fromisoformat(request['travel_date']),
                ('WHN','XAY'),(request['origin_scope'],request['destination_scope']),request['train_preference'],request['departure_window'])
            payload={'status':'ok' if trains else 'empty','trains':trains[:6],'all_trains':trains,
                'request':request,'retrieved_at':FIXTURE['retrieved_at'],'candidate_truncated':False}
            return ToolResult('completed',name,data='已查询车次，数据来自固定回放。',payload=payload)
        self.tool=TravelQueryTool(store=self.web.store,tool_executor=execute)

    def seed(self,window='全天',legacy=False):
        w=self.web; text=f'2026-11-01武汉到西安{window}的高铁'
        event=w.event(text,'rail-'+window)
        values={'origin':'武汉','destination':'西安','travel_date':'2026-11-01','origin_scope':'city',
                'destination_scope':'city','train_preference':'high_speed_only','train_preference_text':'高铁','departure_window':window}
        ir=IntentIR(1,'read',(IntentOperation('rail','query',values),),source_spans=(text,))
        reply=self.tool.prepare(event,w.store.delivery.begin_event(event.event_key),ir)
        platform=w.repo.deliver(OutgoingMessage('group',w.identity,event.event_id,{'kind':'reply','text':reply},'card-'+window))
        with w.store._connect() as db:
            db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',('web',event.storage_scope_id,platform,event.event_key,event.sender_id))
            if legacy:
                for row in db.execute("SELECT operation_key,result_text FROM event_tool_results WHERE event_id=? AND tool_name='query_train_tickets'",(event.event_key,)).fetchall():
                    data=json.loads(row['result_text']); data['payload'].pop('all_trains',None)
                    db.execute('UPDATE event_tool_results SET result_text=? WHERE operation_key=?',(json.dumps(data),row['operation_key']))
        if legacy:
            w.store.delivery.save_event_tool_result('rpc-'+window,event.event_key,'rail:get-tickets',FIXTURE['request'],json.dumps({
                'retrieved_at':FIXTURE['retrieved_at'],'result':{'content':[{'type':'text','text':json.dumps(FIXTURE['trains'])}],'isError':False}}))
        refs=next(item for item in w.messages() if item['id']=='d:'+platform[4:])['rail_results']
        self.assertEqual(len(refs),1,'Duplicate tool checkpoints must not create duplicate cards')
        return refs[0]['id']

    def get(self,ref,query=''):
        return self.web.client.get(f'/api/conversations/{self.web.identity}/rail-results/{ref}{query}')

    def test_complete_snapshot_is_grouped_and_paged_through_the_evening(self):
        ref=self.seed(); first=self.get(ref).json()
        self.assertEqual((first['total_count'],first['page_count'],len(first['items'])),(34,6,6))
        self.assertEqual(first['time_span'],['07:18','19:21'])
        self.assertEqual(first['queried_at'],FIXTURE['retrieved_at'])
        self.assertEqual(first['items'][0]['train_code'],'G3901')
        self.assertEqual(len(first['items'][0]['options']),2)
        before=len(self.calls)
        last=self.get(ref,'?page=6').json()
        self.assertIn('G411',[item['train_code'] for item in last['items']])
        evening=self.get(ref,'?period=evening').json()
        self.assertTrue(evening['items'])
        self.assertTrue(all(option['departure_at'][11:16]>='18:00' for group in evening['items'] for option in group['options']))
        self.assertEqual(len(self.calls),before,'Reading pages and filters must not query the provider')
        self.assertEqual(self.get(ref,'?page=99').status_code,422)
        self.assertEqual(self.get(ref,'?period=unknown').status_code,422)

    def test_morning_snapshot_never_exposes_unqueried_hours(self):
        ref=self.seed('上午'); data=self.get(ref).json()
        self.assertEqual(data['request']['departure_window'],[9,12])
        self.assertEqual(data['filters'],[{'id':'current','label':'已查时段'}])
        self.assertTrue(all('09:00'<=option['departure_at'][11:16]<'12:00' for group in data['items'] for option in group['options']))
        self.assertEqual(self.get(ref,'?period=evening').status_code,422)
        self.assertIn('all_day',[action['period'] for action in data['query_actions']])

    def test_requested_seat_drives_the_card_quote_without_changing_the_saved_snapshot(self):
        ref=self.seed('上午')
        with self.web.store._connect() as db:
            rows=db.execute("SELECT operation_key,result_text FROM event_tool_results WHERE tool_name='query_train_tickets'").fetchall()
            for row in rows:
                value=json.loads(row['result_text']);value['payload']['request']['seat_class']='first_class'
                for train in value['payload']['all_trains']:
                    train['seats'].append({'name':'一等座','price_cny':399,'availability':'available'})
                db.execute('UPDATE event_tool_results SET result_text=? WHERE operation_key=?',(json.dumps(value),row['operation_key']))
        before=len(self.calls)
        result=self.get(ref).json()
        self.assertTrue(result['items'])
        self.assertTrue(all(option['quoted_seat']['name']=='一等座' for group in result['items'] for option in group['options']))
        self.assertEqual(len(self.calls),before)

    def test_saved_old_morning_window_is_not_reinterpreted_when_reading(self):
        ref=self.seed('上午');before=len(self.calls)
        with self.web.store._connect() as db:
            rows=db.execute("SELECT operation_key,result_text FROM event_tool_results WHERE tool_name='query_train_tickets'").fetchall()
            for row in rows:
                saved=json.loads(row['result_text']);saved['payload']['request']['departure_window']=[0,12]
                db.execute('UPDATE event_tool_results SET result_text=? WHERE operation_key=?',(json.dumps(saved),row['operation_key']))
        self.assertEqual(self.get(ref).json()['request']['departure_window'],[0,12])
        self.assertEqual(len(self.calls),before)

    def test_planning_dependency_does_not_replace_itinerary_with_standalone_cards(self):
        self.seed()
        with self.web.store._connect() as db:
            db.execute("UPDATE travel_tasks SET task_type='itinerary'")
        reply = next(item for item in self.web.messages() if item['role'] == 'assistant')
        self.assertNotIn('rail_results', reply)
        self.assertIn('已查询车次', reply['content'])

    def test_legacy_reply_restores_exact_raw_receipt_without_new_queries(self):
        ref=self.seed(legacy=True); before=len(self.calls)
        data=self.get(ref,'?page=6').json()
        self.assertFalse(data['legacy_partial'])
        self.assertEqual(data['total_count'],34)
        self.assertIn('G411',[item['train_code'] for item in data['items']])
        self.assertEqual(len(self.calls),before)

    def test_missing_legacy_rpc_and_provider_cap_are_never_presented_as_complete(self):
        ref=self.seed(legacy=True); w=self.web
        with w.store._connect() as db:
            db.execute("DELETE FROM event_tool_results WHERE tool_name='rail:get-tickets'")
        partial=self.get(ref).json()
        self.assertTrue(partial['legacy_partial'])
        self.assertTrue(partial['incomplete'])
        ref=self.seed('上午')
        with w.store._connect() as db:
            row=db.execute('SELECT result_text FROM event_tool_results WHERE operation_key=?',(ref,)).fetchone()
            data=json.loads(row[0]); data['payload'].update(candidate_truncated=True,candidate_limit=100)
            db.execute('UPDATE event_tool_results SET result_text=? WHERE operation_key=?',(json.dumps(data),ref))
        capped=self.get(ref).json()
        self.assertTrue(capped['incomplete'])
        self.assertEqual(capped['candidate_limit'],100)

    def test_expansion_uses_owned_parameters_and_the_single_read_owner_without_model(self):
        ref=self.seed('上午'); w=self.web
        compiler=Mock(); compiler.try_compile.side_effect=AssertionError('Card actions must not invoke a model')
        w.runtime.application.semantic_task_service=RequestRouter(w.store,compiler,read_only_service=self.tool)
        url=f'/api/conversations/{w.identity}/rail-results/{ref}/query'
        body={'period':'all_day','client_request_id':'expand-day'}
        first=w.client.post(url,headers=w.headers,json=body)
        self.assertEqual(first.status_code,202,first.text)
        self.assertEqual(w.client.post(url,headers=w.headers,json=body).json()['job_id'],first.json()['job_id'])
        self.assertTrue(w.run_job())
        self.assertEqual(self.calls[-1]['departure_window'],'00:00至24:00')
        self.assertEqual(self.calls[-1]['travel_date'],'2026-11-01')
        self.assertEqual(self.calls[-1]['train_preference'],'high_speed_only')
        self.assertEqual((self.calls[-1]['origin'],self.calls[-1]['destination']),('武汉','西安'))
        compiler.try_compile.assert_not_called()
        count=len(self.calls)
        self.assertEqual(w.client.post(url,headers=w.headers,json=body).status_code,202)
        self.assertEqual(len(self.calls),count)
        self.assertEqual(w.client.post(url,headers=w.headers,json={**body,'period':'afternoon'}).status_code,409)

    def test_other_conversation_and_untyped_message_cannot_supply_card_actions(self):
        ref=self.seed(); w=self.web
        other=w.client.post('/api/conversations',headers=w.headers).json()['id']
        self.assertEqual(w.client.get(f'/api/conversations/{other}/rail-results/{ref}').status_code,404)
        self.assertEqual(w.client.post(f'/api/conversations/{other}/rail-results/{ref}/query',headers=w.headers,
            json={'period':'all_day','client_request_id':'cross'}).status_code,404)
        self.assertEqual(w.client.post(f'/api/conversations/{w.identity}/messages',headers=w.headers,
            json={'content':'查询','client_request_id':'forged','rail_query':{'request':{}}}).status_code,422)
