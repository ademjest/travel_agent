from app.conversation_context import web_context
from tools.trip.tool import PlanningArtifacts
import asyncio
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import tempfile
from core.tool_result import ToolResult
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from PIL import Image

from adapters.web_app import create_web_app
from core.chat_transport import ChatEvent, OutgoingMessage
from core.settings import Settings, SettingsError
from core.tasks import TaskUpdate
from core.web_settings import WebSettings
from infrastructure.web_repository import OWNER


class WebTests(unittest.TestCase):
    def rail_quote(self,key='rail-seed'):
        from core.intent_ir import IntentIR,IntentOperation
        event=self.event('出发站到到达站上午出发',key)
        ir=IntentIR(1,'clarify',(IntentOperation('rail','query',{'origin':'出发站','destination':'到达站','departure_window':'上午'}),),
                    missing_fields=('travel_date',),source_spans=(event.content,))
        reply=self.runtime.application.read_only_service.prepare(event,self.store.delivery.begin_event(event.event_key),ir)
        platform_id=self.repo.deliver(OutgoingMessage('group',self.identity,event.event_id,{'kind':'reply','text':reply},'quote-'+key))
        with self.store._connect() as db:
            db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',
                ('web',event.storage_scope_id,platform_id,event.event_key,OWNER))
            db.execute("UPDATE travel_tasks SET expires_at='2000-01-01T00:00:00+00:00' WHERE source_event_id=?",(event.event_key,))
        return 'd:'+platform_id.removeprefix('web-')

    def test_quote_is_scoped_persistent_and_part_of_idempotency(self):
        import json
        from adapters.web_adapter import WebAdapter
        quote=self.rail_quote()
        message=next(m for m in self.messages() if m['id']==quote)
        self.assertEqual(message['reference']['kind'],'rail')
        body={'content':'明天','client_request_id':'quoted','quoted_message_id':quote}
        url=f'/api/conversations/{self.identity}/messages'
        first=self.client.post(url,headers=self.headers,json=body)
        self.assertEqual(first.status_code,202,first.text)
        self.assertEqual(self.client.post(url,headers=self.headers,json=body).json()['job_id'],first.json()['job_id'])
        with self.store._connect() as db:
            payload=json.loads(db.execute('SELECT payload_json FROM inbox_jobs WHERE id=?',(first.json()['job_id'],)).fetchone()[0])
        event=WebAdapter(self.runtime.application,self.repo,context_builder=lambda _:{}).input_event(payload)
        self.assertEqual(event.reply_to_id,'web-'+quote[2:])
        second=self.rail_quote('rail-second')
        conflict=self.client.post(url,headers=self.headers,json={**body,'quoted_message_id':second})
        self.assertEqual(conflict.status_code,409)
        other=self.client.post('/api/conversations',headers=self.headers).json()['id']
        response=self.client.post(f'/api/conversations/{other}/messages',headers=self.headers,json={**body,'client_request_id':'cross'})
        self.assertEqual(response.status_code,422)
        invalid=self.client.post(url,headers=self.headers,json={**body,'client_request_id':'invalid','quoted_message_id':'d:99999999'})
        self.assertEqual(invalid.status_code,422)
        oversized=self.client.post(url,headers=self.headers,json={**body,'client_request_id':'oversized','quoted_message_id':'d:'+'9'*28})
        self.assertEqual(oversized.status_code,422)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = WebSettings(data_dir=Path(self.temp.name), open_browser=False)
        self.app = create_web_app(self.settings, Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        self.repo = self.app.state.repository
        self.store = self.repo.store
        self.worker = self.app.state.worker
        self.runtime = self.app.state.runtime
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8080')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        response = self.client.get('/api/bootstrap')
        self.assertEqual(response.status_code, 200)
        self.headers = {'x-csrf-token': response.json()['csrf_token']}
        self.identity = self.client.post('/api/conversations', headers=self.headers).json()['id']

    def send(self, text, key='message', uploads=None, identity=None):
        return self.client.post(f'/api/conversations/{identity or self.identity}/messages', headers=self.headers,
                                json={'content': text, 'client_request_id': key, 'upload_ids': uploads or []})

    def run_job(self):
        return asyncio.run(self.worker.run_once())

    def messages(self):
        return self.client.get(f'/api/conversations/{self.identity}/messages').json()['items']

    def upload(self, filename, data, identity=None):
        return self.client.post('/api/uploads', headers=self.headers,
            data={'conversation_id': identity or self.identity}, files={'file': (filename, data, 'application/octet-stream')})

    def event(self, text, key='direct'):
        return ChatEvent('web', 'group', key, self.identity, OWNER, text)

    def test_without_qq_config_and_restart_history(self):
        self.assertEqual(self.client.get('/health/ready').status_code, 200)
        sent = self.send('ping')
        self.assertEqual(sent.status_code, 202)
        self.assertTrue(self.run_job())
        self.assertEqual([m['content'] for m in self.messages()], ['ping', 'pong'])
        other = create_web_app(self.settings, Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        self.assertEqual(other.state.repository.messages(self.identity)['items'][-1]['content'], 'pong')
        self.assertEqual(self.store.conversations.get_recent_turns(f'web:{self.identity}', OWNER)[-1].assistant_content, 'pong')

    def test_generic_quote_accepts_user_and_assistant_text_and_freezes_owned_content(self):
        from adapters.web_adapter import WebAdapter
        sent=self.send('普通聊天里的原始要求',key='quote-user');self.run_job()
        items=self.messages()
        for index,message in enumerate(items):
            body={'content':'继续解释这段内容','client_request_id':'general-'+str(index),'quoted_message_id':message['id']}
            reply=self.client.post(f'/api/conversations/{self.identity}/messages',headers=self.headers,json=body)
            self.assertEqual(reply.status_code,202,reply.text)
            with self.store._connect() as db:
                row=db.execute('SELECT payload_json FROM inbox_jobs WHERE id=?',(reply.json()['job_id'],)).fetchone()
            import json
            event=WebAdapter(self.runtime.application,self.repo,context_builder=lambda _:{}).input_event(json.loads(row['payload_json']))
            self.assertEqual(event.message_reference['content'],message['content'])
            self.assertEqual(event.message_reference['role'],message['role'])
            self.assertNotIn('rail',event.message_reference)
            other=self.client.post('/api/conversations',headers=self.headers).json()['id']
            self.assertEqual(self.client.post(f'/api/conversations/{other}/messages',headers=self.headers,json=body).status_code,422)

    def test_plan_snapshot_and_comparison_are_owned_immutable_and_read_only(self):
        import json
        from tools.trip.tool import TripTool
        event=self.event('规划两天','snapshot')
        claim=self.store.delivery.begin_event(event.event_key)
        trip={'trip_id':'TR-snapshot','version':1,'status':'active','title':'测试两天',
            'spec':{'destination':'乙城','day_count':1},'sources':{'p':{'name':'城市历史馆'}},
            'plan':{'days':[{'day_index':1,'date':'','activities':[{'poi_id':'p','period':'上午','reason':'文化参观'}]}]}}
        reply=TripTool.format_trip(trip,preview=True)+'\n确认后才保存'
        self.store.trips.commit(event,claim,TaskUpdate('itinerary','collecting',event.content,
            {'candidate_trip':trip,'trip_id':trip['trip_id']},('confirmation',)),reply,
            presentation=TripTool.presentation(trip,reply,preview=True))
        for name,value in [('amap',{'status':'completed','plan':trip['plan'],'places':trip['sources']}),
                           ('wendao',{'status':'completed','schedule_text':'日程摘录','days_grounded':False,
                                      'data':{'report':'完整问道文化介绍及行程原文'}}),
                           ('compare',{'status':'completed','choice':'merge','reason':'比较理由','plan':trip['plan']})]:
            self.store.delivery.save_event_tool_result('comparison:'+name+':result',event.event_key,'trip_alternative',{},json.dumps(value))
        self.store.delivery.save_event_tool_result('comparison:wendao:submitted',event.event_key,'trip_alternative',{},'not-json')
        native=self.repo.deliver(OutgoingMessage('group',self.identity,event.event_id,{'kind':'reply','text':reply},'snapshot-reply'))
        with self.store._connect() as db:
            db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',('web',event.storage_scope_id,native,event.event_key,OWNER))
            before=db.execute('SELECT count(*) FROM event_tool_results').fetchone()[0]
            db.execute("UPDATE travel_tasks SET slots_json='{}' WHERE source_event_id=?",(event.event_key,))
        mid='d:'+native.removeprefix('web-')
        message=next(m for m in self.messages() if m['id']==mid)
        self.assertTrue(message['plan_comparison'])
        self.assertIn('城市历史馆',json.dumps(message['trip_presentation'],ensure_ascii=False))
        self.assertEqual(message['trip_presentation']['trip']['spec'],{})
        self.assertEqual(message['trip_presentation']['lifecycle']['state'],'unavailable')
        url=f'/api/conversations/{self.identity}/messages/{mid}/plan-comparison'
        for _ in range(2):
            result=self.client.get(url)
            self.assertEqual(result.status_code,200,result.text)
            data=result.json()
            self.assertEqual(data['branches']['wendao']['data']['report'],'完整问道文化介绍及行程原文')
            self.assertEqual(data['branches']['wendao']['schedule_text'],'日程摘录')
            self.assertEqual(data['final_trip']['spec']['destination'],'乙城')
            self.assertEqual(set(data['branches']),{'amap','wendao','compare'})
        with self.store._connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM event_tool_results').fetchone()[0],before)
        other=self.client.post('/api/conversations',headers=self.headers).json()['id']
        self.assertEqual(self.client.get(url.replace(self.identity,other)).status_code,404)
        self.assertEqual(self.client.get(url.replace(mid,'d:99999999')).status_code,404)

    def test_historical_comparison_never_uses_the_latest_mutable_trip_as_its_preview(self):
        import json
        event=self.event('旧方案','old-comparison');self.store.delivery.begin_event(event.event_key)
        self.store.delivery.save_event_tool_result('old:amap:result',event.event_key,'trip_alternative',{},
            json.dumps({'status':'completed','plan':{'days':[]},'places':{}}))
        native=self.repo.deliver(OutgoingMessage('group',self.identity,event.event_id,{'kind':'reply','text':'旧方案原文'},'old-reply'))
        with self.store._connect() as db:db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',
            ('web',event.storage_scope_id,native,event.event_key,OWNER))
        result=self.repo.plan_comparison(self.identity,'d:'+native.removeprefix('web-'))
        self.assertIsNone(result['final_trip'])
        self.assertIn('未保存独立快照',result['final_note'])
        self.assertEqual(result['final_text'],'旧方案原文')

    def test_full_rail_snapshot_uses_the_inbox_scope_and_checks_ownership(self):
        import json
        from dataclasses import replace
        self.send('查询车票',key='owned-rail')
        event=self.event('查询车票','owned-rail');self.store.delivery.begin_event(event.event_key)
        value={'status':'completed','payload':{'retrieved_at':'2026-10-30T01:00:00Z','all_trains':[]}}
        self.store.delivery.save_event_tool_result('owned-rail',event.event_key,'query_train_tickets',{},json.dumps(value))
        self.assertEqual(self.store.trips.rail_snapshots(event,'2026-10-30T01:00:00Z'),[value['payload']])
        self.assertEqual(self.store.trips.rail_snapshots(replace(event,sender_id='other'),'2026-10-30T01:00:00Z'),[])
        self.assertEqual(self.store.trips.rail_snapshots(replace(event,scope_id='other'),'2026-10-30T01:00:00Z'),[])

    def test_research_progress_can_be_read_before_fact_report_exists(self):
        event = self.event('核对景点', 'research-progress')
        self.store.research.save(event, 'reading', {'fact_schema': 'attraction-facts-v1', 'sources': []})
        response = self.client.get(f'/api/conversations/{self.identity}/research')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['report'], '')

    def test_official_navigation_is_typed_read_only_and_preserves_provider_fact_status(self):
        from test_official_links import navigation_data
        value=navigation_data()
        event=self.event('查看景点资料','official-navigation')
        self.store.research.save(event,'partial',value)
        before=self.store.research.get(event)
        tool=self.runtime.application.research_service
        tool.search=Mock(configured=True)
        tool.reader=Mock(side_effect=AssertionError('Opening a report must not query'))
        response=self.client.get(f'/api/conversations/{self.identity}/research')
        self.assertEqual(response.status_code,200)
        item=response.json()[0]['presentation']['entries'][0]
        self.assertEqual(item['official_link']['status'],'verified_navigation')
        self.assertEqual(response.json()[0]['presentation']['metrics']['official_verified_count'],0)
        self.assertIn('详细信息见景点官网',response.json()[0]['report'])
        self.assertEqual(self.client.get(f'/api/conversations/{self.identity}/research').json(),response.json())
        self.assertEqual(self.store.research.get(event),before)
        tool.search.search.assert_not_called();tool.reader.assert_not_called()
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inbox_jobs').fetchone()[0],0)

    def test_legacy_full_research_report_is_projected_without_rewriting_history(self):
        from test_attraction_projection import sample_data
        event = self.event('核对景点', 'research-full')
        data = sample_data()
        data['report'] = '旧报告原文，不应被覆盖'
        self.store.research.save(event, 'partial', data)
        response = self.client.get(f'/api/conversations/{self.identity}/research')
        self.assertEqual(response.status_code, 200)
        self.assertIn('来源与核验依据', response.json()[0]['report'])
        self.assertIn('景点19', response.json()[0]['report'])
        self.assertEqual(self.store.research.get(event)['report'], data['report'])

    def test_conversation_context_includes_scoped_usage_and_protects_missing_or_deleted_scopes(self):
        response=self.client.get(f'/api/conversations/{self.identity}/context')
        usage=response.json()['model_usage']
        self.assertEqual(usage['conversation_id'],self.identity)
        self.assertEqual(usage['totals']['total_tokens'],0)
        self.assertEqual(usage['totals']['calls'],0)
        with TestClient(self.app,base_url='http://127.0.0.1:8080') as anonymous:
            self.assertEqual(anonymous.get(f'/api/conversations/{self.identity}/context').status_code,401)
        self.assertEqual(self.client.get('/api/conversations/unknown-usage/context').status_code,404)
        self.client.delete(f'/api/conversations/{self.identity}',headers=self.headers)
        self.assertEqual(self.client.get(f'/api/conversations/{self.identity}/context').status_code,410)

    def test_idempotent_submission_and_different_body_conflict(self):
        first = self.send('ping').json()
        self.assertEqual(self.send('ping').json()['job_id'], first['job_id'])
        self.assertEqual(self.send('别的内容').status_code, 409)
        self.run_job()
        self.assertFalse(self.run_job())
        self.assertEqual(len(self.messages()), 2)

    def test_loopback_host_origin_session_and_csrf(self):
        self.assertEqual(self.client.post('/api/conversations').status_code, 403)
        self.assertEqual(self.client.get('/api/bootstrap', headers={'Host': 'evil.example:8080'}).status_code, 403)
        self.assertEqual(self.client.get('/api/bootstrap', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/bootstrap', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.client.post('/api/conversations', headers={**self.headers, 'Origin': 'http://localhost:9999'}).status_code, 403)
        with TestClient(self.app, base_url='http://127.0.0.1:8080') as anonymous:
            self.assertEqual(anonymous.get('/api/conversations').status_code, 401)
        with self.assertRaises(SettingsError):
            WebSettings(host='0.0.0.0').validate()
        from core.data_paths import PROJECT_ROOT
        with self.assertRaises(SettingsError):
            WebSettings(data_dir=PROJECT_ROOT / 'data').validate()

    def test_upload_scope_format_and_size_validation(self):
        u = self.upload('攻略.md', '# 武汉\n湖北省博物馆'.encode()).json()
        other = self.repo.create_conversation()['id']
        self.assertEqual(self.send('资料', uploads=[u['id']], identity=other).status_code, 404)
        self.assertEqual(self.upload('fake.png', b'not an image').status_code, 415)
        self.assertEqual(self.upload('bad.exe', b'a').status_code, 415)
        self.assertEqual(self.upload('too-big.txt', b'a' * (5*1024*1024+1)).status_code, 413)
        self.assertEqual(self.send('hello', uploads=['../../.env']).status_code, 404)
        self.assertEqual(self.client.get('/api/uploads/unknown').status_code, 404)
        self.assertEqual(self.client.get('/api/uploads/'+u['id']).status_code, 200)

    def test_document_import_and_isolation(self):
        uploaded = self.upload('武汉攻略.md', '# 武汉\n湖北省博物馆需要预约。'.encode()).json()
        self.assertEqual(self.send('', uploads=[uploaded['id']]).status_code, 202)
        self.run_job()
        ctx = self.client.get(f'/api/conversations/{self.identity}/context').json()
        self.assertEqual(ctx['documents'][0]['filename'], '武汉攻略.md')
        other = self.repo.create_conversation()['id']
        self.assertEqual(web_context(self.repo, other)['documents'], ())
        self.assertEqual(self.messages()[-1]['role'], 'assistant')
        self.assertNotIn('绑定码', self.messages()[-1]['content'])

    def test_planning_artifacts_are_visible_selectable_and_deletable_by_version(self):
        artifact=PlanningArtifacts(self.store.planning).capture(self.event('规划开封2天行程','plan-source'),
            '开封参考方案\n第1天：清明上河园。第2天：开封府。')
        plans=web_context(self.repo, self.identity)['planning_artifacts']
        self.assertEqual(plans[0]['artifact_id'],artifact['artifact_id'])
        response=self.client.delete(f'/api/conversations/{self.identity}/plans/{artifact["artifact_id"]}?version=99',headers=self.headers)
        self.assertEqual(response.status_code,409)
        response=self.client.delete(f'/api/conversations/{self.identity}/plans/{artifact["artifact_id"]}?version={artifact["version"]}',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(web_context(self.repo, self.identity)['planning_artifacts'],[])

    def test_image_uses_original_image_service_and_scoped_local_attachment(self):
        data = io.BytesIO()
        Image.new('RGB', (12, 12), 'white').save(data, 'PNG')
        image = self.upload('票据.png', data.getvalue()).json()
        service = self.runtime.application.image_context_service
        service.analyze = Mock(return_value={'uncertain': False, 'answer': '预约日期为10月1日', 'facts': '预约日期', 'media_id': 1})
        self.send('这张票是什么日期？', uploads=[image['id']])
        self.run_job()
        self.assertIn('10月1日', self.messages()[-1]['content'])
        attachment = service.analyze.call_args.args[1][0]
        self.assertTrue(Path(attachment.local_path).resolve().is_relative_to(Path(self.temp.name).resolve()))
        self.assertEqual(attachment.url, '')

    def test_platform_filtered_claim_does_not_take_onebot_work(self):
        self.store.inbox.submit('onebot:group:real:1', 'onebot', 'real', 'qq-user', {'message': []})
        self.send('ping')
        self.run_job()
        self.assertEqual(self.store.inbox.get('onebot:group:real:1')['status'], 'pending')
        self.assertFalse(self.run_job())
        self.store.inbox.submit('onebot:group:real:2', 'onebot', 'real', 'qq-user', {'message': []}, has_assets=True)
        self.assertIsNone(self.store.inbox.claim_capture(platform='web'))

    def test_output_replay_after_save_before_ack_is_deduplicated(self):
        message = OutgoingMessage('group', self.identity, 'request', {'text': '一次回复', 'kind': 'reply'}, 'web:outbox:123')
        first = self.repo.deliver(message)
        self.assertEqual(self.repo.deliver(message), first)
        self.assertEqual(len(self.messages()), 1)

    def test_cancel_pending_job_and_reject_unknown_task(self):
        job = self.send('ping').json()['job_id']
        self.assertEqual(self.client.post(f'/api/tasks/{job}/cancel', headers=self.headers).status_code, 200)
        self.assertFalse(self.run_job())
        self.assertEqual(self.client.get(f'/api/tasks/{job}').json()['status'], 'cancelled')
        self.assertEqual(self.client.post('/api/tasks/999/cancel', headers=self.headers).status_code, 404)
        self.assertEqual(len(self.messages()), 1)

    def test_completed_task_cannot_be_cancelled(self):
        job = self.send('ping').json()['job_id']
        self.run_job()
        self.assertEqual(self.client.post(f'/api/tasks/{job}/cancel', headers=self.headers).status_code, 409)

    def test_colloquial_time_reply_continues_reminder_without_chat_fallback(self):
        # A generic chatbot must not handle the follow-up or claim reminders are unavailable.
        self.runtime.travel_service.handle = Mock(side_effect=AssertionError('must stay in reminder service'))
        self.send('明天提醒我预约陕西历史博物馆。', key='reminder-start')
        self.run_job()
        self.assertIn('几点', self.messages()[-1]['content'])
        self.send('10点吧', key='reminder-clock')
        self.run_job()
        self.assertIn('上午还是', self.messages()[-1]['content'])
        self.assertEqual(web_context(self.repo, self.identity)['reminders'], ())
        self.send('上午吧', key='reminder-period')
        self.run_job()
        self.assertIn('10:00', self.messages()[-1]['content'])
        reminders = web_context(self.repo, self.identity)['reminders']
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]['title'], '预约陕西历史博物馆')
        self.runtime.travel_service.handle.assert_not_called()

    def test_web_calls_contextual_llm_for_nonmatching_followup(self):
        from agents.intent_compiler import IntentCompiler
        from app.request_router import RequestRouter
        from test_semantic_reminder_continuation import Client
        model = Client()
        self.runtime.application.semantic_task_service = RequestRouter(
            self.store, IntentCompiler(model, 'test'), reminder_service=self.runtime.application.personal_reminder_service)
        self.runtime.travel_service.handle = Mock(side_effect=AssertionError('must not fall through to generic chat'))
        model.value = {'schema_version': 1, 'action': 'create', 'operations':[
            {'domain':'reminder','operation':'create','title':'预约陕西历史博物馆','trigger':{'text':'明天'}}],
            'source_spans':['明天','预约陕西历史博物馆']}
        self.send('明天提醒我预约陕西历史博物馆', key='semantic-start')
        self.run_job()
        model.value = {'schema_version':1, 'action':'update', 'operations':[
            {'domain':'reminder','operation':'continue_pending','task_index':1,'disposition':'answer',
             'trigger':{'text':'上午十点','source_text':'快到中午的十点整'}}], 'source_spans':['快到中午的十点整']}
        self.send('我希望安排在快到中午的十点整，方便出门前处理', key='semantic-followup')
        self.run_job()
        self.assertIn('10:00', self.messages()[-1]['content'])
        self.assertEqual(len(web_context(self.repo, self.identity)['reminders']), 1)
        self.assertEqual(len(model.requests), 2)
        self.assertIn('预约陕西历史博物馆', model.requests[-1]['context'])
        self.runtime.travel_service.handle.assert_not_called()

    def test_real_reminder_service_and_notification_without_browser(self):
        self.send('1分钟后提醒我带身份证')
        self.run_job()
        rows = self.store.reminders.list_for_owner('web', self.identity, OWNER)
        self.assertEqual(len(rows), 1, self.messages())
        due = datetime.fromisoformat(rows[0]['scheduled_at_utc']) + timedelta(seconds=1)
        asyncio.run(self.runtime.reminder_scheduler.scan_once(due))
        asyncio.run(self.runtime.outbox_worker.dispatch_due_once(due))
        notices = self.repo.notifications()
        self.assertEqual(len(notices), 1)
        self.assertIn('身份证', notices[0]['content'])
        self.assertIsNone(notices[0]['read_at'])
        self.client.post(f'/api/notifications/{notices[0]["id"]}/read', headers=self.headers)
        self.assertIsNotNone(self.repo.notifications()[0]['read_at'])

        with self.store._connect() as connection:
            connection.execute("UPDATE web_deliveries SET created_at=? WHERE id=?",
                               ((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(), notices[0]['id']))
            connection.execute("""INSERT INTO web_deliveries
                (delivery_key,conversation_id,reply_to_id,kind,content,created_at)
                VALUES ('legacy-reminder',?,'','notification','提醒你：旧提醒',?)""",
                (self.identity, (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()))
        self.assertEqual(self.repo.notifications(), [])
        asyncio.run(self.runtime.reminder_scheduler.scan_once(due + timedelta(days=2)))
        self.assertEqual(self.store.reminders.list_for_owner('web', self.identity, OWNER), ())

    def test_versioned_confirmation_rejects_old_preview(self):
        event = self.event('修改行程')
        claim = self.store.delivery.begin_event(event.event_key)
        task = TaskUpdate('itinerary', 'collecting', event.content, {}, ('confirmation',))
        self.store.tasks.prepare_result(event, claim, task, '请核对变更')
        ctx = web_context(self.repo, self.identity)
        preview = ctx['confirmations'][0]
        response = self.client.post(f'/api/conversations/{self.identity}/confirmations/{preview["id"]}',
            headers=self.headers, json={'version': preview['version']+1, 'client_request_id': 'confirm-old'})
        self.assertEqual(response.status_code, 409)
        response = self.client.post(f'/api/conversations/{self.identity}/confirmations/{preview["id"]}',
            headers=self.headers, json={'version': preview['version'], 'client_request_id': 'confirm'})
        self.assertEqual(response.status_code, 202)
        with self.store._connect() as db:
            db.execute('UPDATE travel_tasks SET version=version+1 WHERE task_id=?', (preview['id'],))
        self.runtime.application.trip_service.handle = Mock(side_effect=AssertionError('must not apply stale preview'))
        self.run_job()
        self.assertIn('未执行修改', self.messages()[-1]['content'])

    def delivered_preview(self):
        def prepare(event, claim, *, tasks=None, operation=None):
            reply = '完整预览：行程及关联提醒变更，核对后再确认。'
            self.store.tasks.prepare_result(event,claim,
                TaskUpdate('itinerary','collecting',event.content,{},('confirmation',)),reply)
            return reply
        self.runtime.application.trip_service.handle = Mock(side_effect=prepare)
        self.send('帮我规划武汉行程',key='preview')
        self.run_job()
        return web_context(self.repo, self.identity)['confirmations'][0]

    def test_confirmation_snapshot_survives_completed_delivery_and_restart(self):
        preview = self.delivered_preview()
        with self.store._connect() as db:
            event = db.execute("SELECT p.* FROM travel_tasks t JOIN processed_events p ON p.event_id=t.last_event_id WHERE t.task_id=?",(preview['id'],)).fetchone()
        self.assertEqual(event['status'],'completed')
        self.assertIsNone(event['prepared_reply'])
        self.assertTrue(preview['can_confirm'])
        other = create_web_app(self.settings,Settings('', '', frozenset(), '', '', '', ''),start_workers=False)
        self.assertEqual(web_context(other.state.repository, self.identity)['confirmations'][0]['preview'],preview['preview'])

    def test_legacy_preview_recovers_exact_delivered_event_not_latest_reply(self):
        preview = self.delivered_preview()
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET slots_json=json_remove(slots_json,'$.confirmation_preview') WHERE task_id=?",(preview['id'],))
        self.send('ping',key='unrelated')
        self.run_job()
        restored = web_context(self.repo, self.identity)['confirmations'][0]
        self.assertEqual(restored['preview'],preview['preview'])
        self.assertTrue(restored['can_confirm'])

    def test_missing_preview_disables_confirmation_at_api(self):
        event = self.event('无法恢复的旧预览')
        claim = self.store.delivery.begin_event(event.event_key)
        self.store.tasks.prepare_result(event,claim,TaskUpdate('itinerary','collecting',event.content,{},('confirmation',)),'旧预览')
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET slots_json='{}' WHERE last_event_id=?",(event.event_key,))
            db.execute('UPDATE processed_events SET prepared_reply=NULL WHERE event_id=?',(event.event_key,))
        preview = web_context(self.repo, self.identity)['confirmations'][0]
        self.assertEqual(preview['preview'],'')
        self.assertFalse(preview['can_confirm'])
        response = self.client.post(f'/api/conversations/{self.identity}/confirmations/{preview["id"]}',
            headers=self.headers,json={'version':preview['version'],'client_request_id':'invalid-confirm'})
        self.assertEqual(response.status_code,409)

    def test_scheduled_query_uses_web_platform_and_due_time(self):
        service = self.runtime.application.scheduled_query_service
        service._extract_query = Mock(return_value={'kind': 'weather', 'arguments': {'location': '武汉'}, 'time_text': '1分钟后'})
        self.runtime.travel_service.execute_result = Mock(return_value=ToolResult('completed','get_current_weather',data='武汉：晴'))
        self.send('1分钟后告诉我武汉当前天气')
        self.run_job()
        rows = web_context(self.repo, self.identity)['scheduled_queries']
        self.assertEqual(len(rows), 1, self.messages())
        self.runtime.travel_service.execute_result.assert_not_called()
        due = datetime.fromisoformat(rows[0]['due_at']) + timedelta(seconds=1)
        asyncio.run(self.runtime.reminder_scheduler.scan_once(due))
        service.clock = lambda: due
        asyncio.run(self.worker.run_once(now=due))
        self.assertIn('武汉：晴', self.messages()[-1]['content'])
        self.assertEqual(self.repo.notifications()[0]['kind'], 'notification')

    def test_status_no_secrets_and_api_404_not_html(self):
        status = self.client.get('/api/status')
        self.assertEqual(status.status_code, 200)
        self.assertNotIn('api_key', status.text.lower())
        self.assertEqual(self.client.get('/api/not-found').status_code, 404)
        self.assertEqual(self.client.post('/api/conversations', headers={**self.headers, 'Content-Type': 'application/json'},
                                         content=b'a' * (128*1024+1)).status_code, 413)

    def test_trip_service_creates_real_structured_sidebar_data(self):
        from tools.trip.planner import TripPlanner
        from tools.trip.tool import TripTool
        from test_trips import SPEC, PLACES, plan_value, model_response
        from test_travel_agent import FakeClient
        amap = Mock()
        amap.search_places.return_value = list(PLACES.values())
        client = FakeClient([model_response({**SPEC, 'start_date_text': '2030年10月1日'}), model_response(plan_value())])
        self.runtime.application.trip_service = TripTool(self.store, TripPlanner(client, 'test', amap))
        self.send('帮我规划2030年10月1日开始的武汉三天行程，带老人，必去湖北省博物馆')
        self.run_job()
        previews = web_context(self.repo, self.identity)['trips']
        self.assertEqual(len(previews), 1)
        self.assertTrue(previews[0]['is_preview'])
        self.assertEqual(self.store.trips.list_for_owner(self.event('')), ())
        self.send('确认行程修改', key='create-confirm')
        self.run_job()
        trips = web_context(self.repo, self.identity)['trips']
        self.assertEqual(len(trips), 1, self.messages())
        self.assertEqual(trips[0]['spec']['destination'], '武汉')
        self.assertEqual(len(trips[0]['plan']['days']), 3)
        self.assertEqual(trips[0]['sources']['h']['name'], '湖北省博物馆')

    def test_preliminary_trip_uses_reference_date_then_accepts_exact_date(self):
        from dataclasses import replace
        from unittest.mock import patch
        from tools.trip.planner import TripPlanner
        from core.intent_ir import IntentIR, IntentOperation
        from app.request_router import RequestRouter
        from tools.trip.tool import TripTool
        from test_travel_agent import FakeClient
        from test_trips import PLACES, model_response, plan_value
        now = datetime(2030, 9, 9, 2, tzinfo=timezone.utc)
        places = {key: {**row, 'name': '南京测试景点' + key} for key, row in PLACES.items()}
        places.update(hotel={'id': 'hotel', 'name': '测试酒店', 'type': '住宿服务'},
                      food={'id': 'food', 'name': '测试餐厅', 'type': '餐饮服务'})
        amap = Mock()
        amap.search_places.return_value = list(places.values())
        amap.route_for_pois.return_value = {'duration_seconds': 600}
        extraction = {'destination': '南京', 'duration_text': '两天', 'start_date_text': '后面的某个周末',
                      'hotel': {'value': '住宿', 'source': '住宿', 'requested': True},
                      'dining': {'value': '餐饮', 'source': '餐饮', 'requested': True}}
        plan = {**plan_value((('h', 'a'), ('b', 'c'))), 'hotel_ids': ['hotel'], 'restaurant_ids': ['food']}
        client = FakeClient([model_response(extraction), model_response(plan)])
        trip_service = TripTool(self.store, TripPlanner(client, 'test', amap))
        self.runtime.application.trip_service = trip_service
        compiler = Mock(try_compile=Mock(side_effect=lambda text,**kwargs: IntentIR(1,'create',
            (IntentOperation('trip','create',{'request_text':text}),),source_spans=(text[:80],))))
        self.runtime.application.semantic_task_service = RequestRouter(self.store, compiler,
            trip_service=trip_service, mode='execute')
        original_input = self.worker.adapter.input_event
        with patch.object(self.worker.adapter, 'input_event', side_effect=lambda row: replace(original_input(row), occurred_at=now)):
            self.send('我想去南京玩两天，计划是后面的某个周末，我希望你为我规划一个旅游计划，从每天的出行到住宿以及餐饮。')
            self.run_job()
            self.assertEqual(self.store.trips.list_for_owner(self.event('')), ())
            self.send('确认行程修改', key='create-confirm')
            self.run_job()
            trip = self.store.trips.list_for_owner(self.event(''))[0]
            self.assertEqual(trip['spec']['start_date'], '2030-09-10')
            self.assertEqual(trip['plan']['days'][1]['date'], '2030-09-11')
            self.assertTrue(trip['spec']['date_is_tentative'])
            self.assertIn('日期暂定', self.messages()[-1]['content'])
            self.assertNotIn('具体哪天', self.messages()[-1]['content'])
            self.send('我确定10月25日出发', key='actual-date')
            self.run_job()
            self.assertEqual(self.store.trips.list_for_owner(self.event(''))[0]['spec']['start_date'], '2030-09-10')
            self.send('确认行程修改', key='date-confirm')
            self.run_job()
            updated = self.store.trips.list_for_owner(self.event(''))[0]
            self.assertEqual(updated['spec']['start_date'], '2030-10-25')
            self.assertEqual(updated['plan']['days'][1]['date'], '2030-10-26')
            self.assertFalse(updated['spec']['date_is_tentative'])
            self.assertEqual(len(client.completions.requests), 2)

    def test_policy_monitor_change_delivers_web_notification_and_can_stop(self):
        from tools.reminders.policy import BookingPolicyResolver
        from tools.reminders.tool import RemindersTool
        now = datetime.now(timezone.utc)
        page = ['湖北省博物馆\n个人入馆预约可提前5天。每日0点开始放票。']
        resolver = BookingPolicyResolver(sources={'湖北省博物馆':'https://museum.example/policy'},
            fetch_text=lambda url: page[0], clock=lambda: now)
        service = RemindersTool(self.store, resolver=resolver, clock=lambda: now)
        self.runtime.application.policy_watch_service = service
        self.runtime.application.reminders_tool = service
        ending = now+timedelta(days=5)
        self.send(f'监测湖北省博物馆预约规则，到{ending.year}年{ending.month:02d}月{ending.day:02d}日')
        self.run_job()
        watch = web_context(self.repo, self.identity)['policy_watches'][0]
        now = datetime.fromisoformat(watch['next_check_at'])
        page[0] = page[0].replace('提前5天', '提前7天')
        asyncio.run(self.runtime.reminder_scheduler.scan_once(now))
        asyncio.run(self.worker.run_once(now=now, lane='scheduled'))
        self.assertEqual(len(self.repo.notifications()), 1)
        self.assertIn('提前 7 天', self.repo.notifications()[0]['content'])
        self.send('停止规则监测 '+watch['watch_id'], key='stop')
        self.run_job()
        self.assertEqual(web_context(self.repo, self.identity)['policy_watches'][0]['status'], 'cancelled')

    def test_cancel_running_request_fences_late_business_write(self):
        from threading import Event
        started, release = Event(), Event()
        def slow(text):
            started.set()
            release.wait(5)
            self.store.documents.add_document(f'web:{self.identity}', OWNER, 'late.md', 'digest', 'late write', ['late write'])
            return 'must not publish'
        self.runtime.travel_service.handle = slow
        identity = self.send('slow').json()['job_id']
        async def run():
            execution = asyncio.create_task(self.worker.run_once())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                result = self.client.post(f'/api/tasks/{identity}/cancel', headers=self.headers)
                self.assertEqual(result.status_code, 200)
            finally:
                release.set()
                await execution
        asyncio.run(run())
        self.assertEqual(web_context(self.repo, self.identity)['documents'], ())
        self.assertNotIn('must not publish', str(self.messages()))

    def test_expired_lease_recovers_prepared_result_without_reexecution(self):
        job_id = self.send('ping').json()['job_id']
        old = self.store.inbox.claim(platform='web')
        claim = self.store.delivery.begin_event(old['event_key'])
        self.store.delivery.prepare_event_reply(claim.event_id, claim.claim_token, 'saved before restart', 'ping')
        with self.store._connect() as db:
            db.execute('UPDATE inbox_jobs SET lease_until=? WHERE id=?',
                       ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), job_id))
        self.runtime.travel_service.handle = Mock(side_effect=AssertionError('no reexecution'))
        self.run_job()
        self.assertEqual(self.messages()[-1]['content'], 'saved before restart')

    def test_only_one_runtime_can_own_the_data_directory(self):
        from core.web_settings import web_data_lease
        with web_data_lease(self.settings.data_dir):
            with self.assertRaises(SettingsError):
                with web_data_lease(self.settings.data_dir):
                    pass

    def test_invalid_office_file_is_rejected_without_server_error(self):
        self.assertEqual(self.upload('invalid.docx', b'not a ZIP archive').status_code, 415)


if __name__ == '__main__':
    unittest.main()
