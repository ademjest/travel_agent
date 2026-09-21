import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
from threading import Barrier, Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

from adapters.web_app import create_web_app
from core.chat_transport import ChatEvent, OutgoingMessage
from core.settings import Settings
from core.tasks import TaskUpdate
from core.web_lifecycle import ConversationDeleted, publish_files
from core.web_settings import WebSettings
from infrastructure.web_repository import OWNER, WebRepository
from services.conversation_deletion import ConversationDeletionService


class ConversationDeletionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.settings = WebSettings(data_dir=self.root, open_browser=False)
        self.app = create_web_app(self.settings, Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8080')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.headers = {'x-csrf-token': self.client.get('/api/bootstrap').json()['csrf_token']}
        self.repo, self.service, self.worker = self.app.state.repository, self.app.state.deletion, self.app.state.worker
        self.store, self.runtime = self.repo.store, self.app.state.runtime
        self.a = self.repo.create_conversation('测试 A')['id']
        self.b = self.repo.create_conversation('测试 B')['id']

    def send(self, text, identity=None, key='message', uploads=()):
        response = self.client.post(f'/api/conversations/{identity or self.a}/messages', headers=self.headers,
            json={'content': text, 'client_request_id': key, 'upload_ids': list(uploads)})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['job_id']

    def upload(self, identity=None, data=b'# test notes', filename='notes.md'):
        response = self.client.post('/api/uploads', headers=self.headers, data={'conversation_id': identity or self.a},
            files={'file': (filename, data, 'application/octet-stream')})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def delete(self, identity=None):
        response = self.client.delete(f'/api/conversations/{identity or self.a}', headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def finish(self, identity=None):
        state = self.service.cleanup(identity or self.a)
        self.assertEqual(state['state'], 'completed', state)
        with self.store._connect() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_confirmation_summary_no_side_effect_and_csrf_required(self):
        self.send('ping')
        result = self.client.get(f'/api/conversations/{self.a}/deletion-summary').json()
        self.assertEqual(result['active_tasks'], 1)
        self.assertEqual(result['messages'], 1)
        self.assertTrue(self.repo.allows(self.a))
        self.assertEqual(self.client.delete(f'/api/conversations/{self.a}').status_code, 403)
        self.assertEqual(self.client.delete(f'/api/conversations/{self.a}', headers={**self.headers,'Origin':'https://other.example'}).status_code, 403)
        self.assertEqual(self.service.list_jobs(), [])

    def test_delete_empty_conversation_is_idempotent_and_does_not_touch_other(self):
        first = self.delete()
        self.assertEqual(self.delete()['requested_at'], first['requested_at'])
        self.assertEqual(self.client.get(f'/api/conversations/{self.a}/messages').status_code, 410)
        self.assertEqual([item['id'] for item in self.repo.conversations()], [self.b])
        self.finish()
        self.assertEqual(self.delete()['state'], 'completed')
        self.assertTrue(self.repo.allows(self.b))
        with self.store._connect() as db:
            marker = dict(db.execute('SELECT * FROM web_conversation_deletions').fetchone())
            self.assertEqual(marker['files_json'], '[]')
            self.assertEqual(marker['events_json'], '[]')
            self.assertNotIn('测试 A', str(marker))

    def test_history_document_unsubmitted_upload_and_replayed_request_are_removed(self):
        doc = self.upload()
        unused = self.upload(data=b'unsubmitted', filename='other.txt')
        paths = [self.root / self.repo.upload(item)['relative_path'] for item in (doc, unused)]
        self.send('', uploads=(doc,))
        asyncio.run(self.worker.run_once())
        self.send('ping', self.b, 'other-conversation')
        asyncio.run(self.worker.run_once())
        self.delete()
        self.finish()
        self.assertTrue(all(not path.exists() for path in paths))
        for endpoint in ('messages', 'context', 'deletion-summary'):
            self.assertEqual(self.client.get(f'/api/conversations/{self.a}/{endpoint}').status_code, 410)
        self.assertEqual(self.client.post(f'/api/conversations/{self.a}/messages',headers=self.headers,
            json={'content':'','client_request_id':'message','upload_ids':[doc]}).status_code, 410)
        self.assertEqual(self.client.post(f'/api/conversations/{self.a}/messages',headers=self.headers,
            json={'content':'late','client_request_id':'late'}).status_code, 410)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM documents WHERE group_openid=?',('web:'+self.a,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM document_chunks').fetchone()[0], 0)
            if self.store._document_fts_available:
                self.assertEqual(db.execute('SELECT count(*) FROM document_chunks_fts').fetchone()[0], 0)
        self.assertEqual(self.repo.messages(self.b)['items'][-1]['content'], 'pong')

    def test_enqueued_and_sending_reminders_cannot_deliver_after_boundary(self):
        self.send('10分钟后提醒我带身份证')
        asyncio.run(self.worker.run_once())
        reminder = self.repo.context(self.a)['reminders'][0]
        due = datetime.fromisoformat(reminder['scheduled_at_utc'])+timedelta(seconds=1)
        asyncio.run(self.runtime.reminder_scheduler.scan_once(due))
        row = self.store.list_due_outbox('web', due)[0]
        self.assertIsNotNone(self.store.claim_outbox(row.outbox_id, due))
        self.delete()
        # This imitates a sender that cached its message before deletion was requested.
        with self.assertRaises(ConversationDeleted):
            asyncio.run(self.runtime.outbox_worker.transport.send(OutgoingMessage(
                'group',self.a,'',row.payload, f'web:outbox:{row.outbox_id}')))
        self.assertEqual(asyncio.run(self.runtime.reminder_scheduler.scan_once(due)), 0)
        self.assertEqual(self.repo.notifications(), [])
        self.finish()
        asyncio.run(self.runtime.outbox_worker.dispatch_due_once(due))
        with self.store._connect() as db:
            for table in ('personal_reminders','reminder_occurrences','outbox_messages','processed_events','travel_tasks','task_event_results'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0, table)

    def test_running_model_cannot_publish_file_or_result_after_delete(self):
        data = io.BytesIO(); Image.new('RGB',(12,12),'white').save(data,'PNG')
        upload = self.upload(data=data.getvalue(),filename='test.png')
        started, release = Event(), Event()
        def response(**kwargs):
            started.set(); release.wait(5)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
                {'facts':'测试','answer':'迟到回复','uncertain':False},ensure_ascii=False)))])
        self.runtime.application.image_context_service.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=response)))
        self.send('识别这张图片', uploads=(upload,))
        async def run():
            job = asyncio.create_task(self.worker.run_once())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,2))
                await asyncio.to_thread(self.service.request,self.a)
                await asyncio.to_thread(self.finish)
            finally:
                release.set(); await job
        asyncio.run(run())
        with self.store._connect() as db:
            for table in ('media_observations','web_deliveries','processed_events','inbox_jobs'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0],0,table)
        self.assertEqual(list(self.root.rglob('*.png')), [])
        self.send('ping',self.b,'after-delete')
        self.assertTrue(asyncio.run(self.worker.run_once()))

    def test_file_permission_failure_is_visible_and_resumes_from_persisted_manifest(self):
        uploaded = self.upload()
        path = (self.root / self.repo.upload(uploaded)['relative_path']).resolve()
        self.delete()
        original = Path.unlink
        def locked(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError('fixture file lock')
            return original(candidate, *args, **kwargs)
        with patch.object(Path,'unlink',locked):
            self.assertEqual(self.service.cleanup(self.a)['state'],'failed')
        self.assertFalse(self.repo.allows(self.a))
        self.assertTrue(path.exists())
        restarted = ConversationDeletionService(WebRepository(self.store))
        self.assertEqual(restarted.list_jobs()[0]['state'],'failed')
        restarted.retry(self.a)
        self.assertEqual(restarted.cleanup(self.a)['state'],'completed')
        self.assertFalse(path.exists())

    def test_restart_after_boundary_recovers_and_duplicate_cleanup_is_safe(self):
        uploaded = self.upload()
        path = self.root / self.repo.upload(uploaded)['relative_path']
        self.delete()
        restarted = ConversationDeletionService(WebRepository(self.store))
        self.assertEqual(restarted.cleanup(self.a)['state'],'completed')
        self.assertEqual(restarted.cleanup(self.a)['state'],'completed')
        self.assertFalse(path.exists())

    def image_record(self, identity, path):
        return self.store.create_reservation_image(storage_scope_id='web:'+identity,platform='web',group_id=identity,
            uploader_id=OWNER,sha256='same-digest',file_path=str(path),content_type='image/png',byte_size=5,model_id='test')[0]

    def test_shared_image_is_removed_only_after_last_conversation_reference(self):
        path = self.root/'images'/'aa'/'shared.png'; path.parent.mkdir(parents=True); path.write_bytes(b'image')
        self.image_record(self.a,path); self.image_record(self.b,path)
        self.delete(); self.finish()
        self.assertTrue(path.exists())
        self.assertEqual(self.store.get_reservation_image('web:'+self.b,'same-digest').file_path,str(path))
        self.delete(self.b); self.finish(self.b)
        self.assertFalse(path.exists())

    def test_path_outside_web_root_is_never_unlinked(self):
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside)/'keep.png'; path.write_bytes(b'do not remove')
            self.image_record(self.a,path)
            self.delete()
            self.assertEqual(self.service.cleanup(self.a)['state'],'failed')
            self.assertEqual(path.read_bytes(),b'do not remove')

    def test_failed_publication_intent_is_cleaned_without_a_registered_attachment(self):
        path = self.root/'inbox-assets'/'web'/'interrupted.part'
        with self.assertRaises(OSError), publish_files(self.store,'web',self.a,(path,)):
            path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'interrupted upload')
            raise OSError('interrupted publication')
        self.delete(); self.finish()
        self.assertFalse(path.exists())
        with self.assertRaises(ConversationDeleted), publish_files(self.store,'web',self.a,(path,)):
            path.write_bytes(b'must not return')

    def test_deletion_waits_only_for_active_local_publication(self):
        entered, release = Event(), Event()
        path = self.root/'inbox-assets'/'web'/'in-progress.txt'
        def publish():
            with publish_files(self.store,'web',self.a,(path,)):
                path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'content')
                entered.set(); release.wait(5)
                self.repo.save_upload('test-upload',self.a,'in-progress.txt',str(path.relative_to(self.root)), 'text/plain',b'content')
        with ThreadPoolExecutor(2) as pool:
            writer = pool.submit(publish)
            self.assertTrue(entered.wait(2))
            deleting = pool.submit(self.service.request,self.a)
            try:
                self.assertFalse(deleting.done())
            finally:
                release.set()
            writer.result(); deleting.result()
        self.finish()
        self.assertFalse(path.exists())

    def test_claimed_request_at_deleted_adapter_does_not_restart_worker(self):
        self.send('ping')
        original = self.worker.adapter.normalize_for_inbox
        def delete_first(payload):
            self.service.request(self.a)
            return original(payload)
        self.worker.adapter.normalize_for_inbox = delete_first
        self.assertTrue(asyncio.run(self.worker.run_once()))
        self.finish()
        self.worker.adapter.normalize_for_inbox = original
        self.send('ping',self.b,'b')
        self.assertTrue(asyncio.run(self.worker.run_once()))

    def test_full_graph_purge_including_trip_reservation_and_scheduled_jobs(self):
        event = ChatEvent('web','group','fixture-trip',self.a,OWNER,'创建行程')
        self.store.trips.commit(event,self.store.begin_event(event.event_key),
            TaskUpdate('itinerary','completed',event.content,{}),'saved',trip={
                'trip_id':'TR-fixture','title':'测试行程','status':'active','spec':{},'plan':{'days':[]},'sources':{}})
        from infrastructure.model_gateway import ModelGateway
        gateway = ModelGateway(self.store, Mock())
        call_id = gateway._reserve(event.event_key, 'fixture-model', 10, 10)
        with self.store._connect() as db:
            db.execute('INSERT INTO event_tool_results VALUES (?,?,?,?,?,?)',
                ('fixture-operation',event.event_key,'fixture_tool','{}','test result',datetime.now(timezone.utc).isoformat()))
        path = self.root/'images'/'aa'/'sample.png'; path.parent.mkdir(parents=True); path.write_bytes(b'image')
        image = self.image_record(self.a,path)
        extraction = SimpleNamespace(attraction_name='测试博物馆',price_text='',opening_hours='',booking_channel='',
            source_text='测试',confidence=1,requires_reservation=True,advance_value=1,advance_unit='days')
        now = datetime.now(timezone.utc)
        draft = self.store.create_reservation_draft(image.image_id,'web',self.a,OWNER,(
            {'extraction':extraction,'visit_date':(now+timedelta(days=2)).date(),'booking_date':(now+timedelta(days=1)).date(),
             'date_candidates':(),'custom_reminder_times':(),'reminder_policy':'default','status':'ready'},))
        self.store.confirm_reservation_plan('web',self.a,OWNER,draft.plan_code,
            {draft.items[0].item_id:(SimpleNamespace(scheduled_at_utc=now+timedelta(minutes=10),is_custom=True),)})
        self.runtime.application.scheduled_query_service._extract = Mock(return_value={
            'kind':'weather','arguments':{'location':'武汉'},'time_text':'10分钟后'})
        self.send('10分钟后告诉我武汉天气',key='schedule'); asyncio.run(self.worker.run_once())
        from services.booking_policy import BookingPolicyResolver
        from services.policy_watch_service import PolicyWatchService
        resolver = BookingPolicyResolver(sources={'湖北省博物馆':'https://museum.example/policy'},
            fetch_text=lambda url:'湖北省博物馆\n个人入馆预约可提前5天。每日0点开始放票。')
        self.runtime.application.policy_watch_service = PolicyWatchService(self.store,resolver)
        ending = now+timedelta(days=3)
        self.send(f'监测湖北省博物馆预约规则，到{ending.year}年{ending.month:02d}月{ending.day:02d}日',key='watch')
        asyncio.run(self.worker.run_once())
        asyncio.run(self.runtime.reminder_scheduler.scan_once(now+timedelta(hours=2)))
        self.delete(); self.finish()
        gateway._finish(call_id,time.monotonic(),None,None)
        self.assertFalse(asyncio.run(self.worker.run_once(now=now+timedelta(hours=2))))
        self.assertEqual(asyncio.run(self.runtime.reminder_scheduler.scan_once(now+timedelta(days=1))),0)
        with self.store._connect() as db:
            for table in ('trips','trip_versions','travel_tasks','task_event_results','reservation_images','reservation_plans',
                          'reservation_items','reservation_reminders','scheduled_queries','policy_watches','inbox_jobs',
                          'outbox_messages','event_tool_results','processed_events','model_calls'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0],0,table)
            self.assertGreater(db.execute('SELECT count(*) FROM booking_policy_evidence').fetchone()[0],0)
        self.assertFalse(path.exists())

    def test_database_failure_rolls_back_content_purge_but_keeps_boundary(self):
        uploaded = self.upload()
        path = self.root/self.repo.upload(uploaded)['relative_path']
        self.delete()
        original = self.service._purge_records
        def fail_after_delete(*args):
            original(*args)
            raise RuntimeError('transaction failure')
        with patch.object(self.service,'_purge_records',fail_after_delete):
            self.assertEqual(self.service.cleanup(self.a)['state'],'failed')
        self.assertTrue(path.exists())
        self.assertFalse(self.repo.allows(self.a))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM web_uploads WHERE conversation_id=?',(self.a,)).fetchone()[0],1)
        self.service.retry(self.a); self.finish()

    def test_restart_after_partial_file_cleanup_repeats_safely(self):
        a, b = self.upload(), self.upload(data=b'other',filename='other.txt')
        paths = [self.root/self.repo.upload(item)['relative_path'] for item in (a,b)]
        self.delete()
        original = self.repo.lifecycle.remove_unreferenced
        count = [0]
        def crash(path):
            count[0] += 1
            if count[0] == 2:
                raise SystemExit('simulated process exit')
            return original(path)
        with patch.object(self.repo.lifecycle,'remove_unreferenced',crash), self.assertRaises(SystemExit):
            self.service.cleanup(self.a)
        self.assertEqual(self.service.status(self.a)['state'],'cleaning')
        self.assertEqual(sum(path.exists() for path in paths),1)
        restarted = ConversationDeletionService(WebRepository(self.store))
        self.assertEqual(restarted.cleanup(self.a)['state'],'completed')
        self.assertFalse(any(path.exists() for path in paths))

    def test_failure_notice_cannot_recreate_events_after_deletion(self):
        self.send('ping')
        self.delete(); self.finish()
        asyncio.run(self.worker._reply('inbox-notice:web:group:'+self.a+':message',self.a,OWNER,
                    {'content':'ping','request_id':'message'},'late failure',synthetic=True))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM processed_events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM outbox_messages').fetchone()[0],0)

    def test_other_platform_same_raw_scope_and_shared_public_evidence_survive(self):
        event = ChatEvent('onebot','group','qq-event',self.a,'qq-owner','10分钟后提醒我带水')
        self.runtime.application.personal_reminder_service.handle(event,self.store.begin_event(event.event_key))
        self.store.add_document('onebot:'+self.a,'qq-owner','qq.md','qq-digest','QQ data',['QQ data'])
        self.delete(); self.finish()
        self.assertEqual(len(self.store.reminders.list_for_owner('onebot',self.a,'qq-owner')),1)
        self.assertEqual(len(self.store.list_document_contents('onebot:'+self.a)),1)

    def test_concurrent_deletions_share_one_durable_operation(self):
        barrier = Barrier(2)
        def request():
            barrier.wait(timeout=2)
            return self.service.request(self.a)
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(request), pool.submit(request)
            a, b = first.result(), second.result()
        self.assertEqual(a['requested_at'],b['requested_at'])
        self.finish()
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM web_conversation_deletions').fetchone()[0],1)

    def test_junction_or_symlink_cannot_redirect_cleanup_outside_root(self):
        import subprocess
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            marker = target/'private.png'; marker.write_bytes(b'outside')
            (self.root/'images').mkdir()
            link = self.root/'images'/'redirect'
            if os.name == 'nt':
                quote = lambda value: "'"+str(value).replace("'","''")+"'"
                result = subprocess.run(['powershell.exe','-NoProfile','-Command',
                    'New-Item -ItemType Junction -Path '+quote(link)+' -Target '+quote(target)+' | Out-Null'],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            else:
                link.symlink_to(target,target_is_directory=True)
            try:
                self.image_record(self.a,link/'private.png')
                self.delete()
                self.assertEqual(self.service.cleanup(self.a)['state'],'failed')
                self.assertEqual(marker.read_bytes(),b'outside')
            finally:
                # Remove only the test link, never traverse its target.
                if os.name == 'nt':
                    os.rmdir(link)
                else:
                    link.unlink()


if __name__ == '__main__':
    unittest.main()
