from copy import deepcopy
import unittest
from unittest.mock import patch

from core.tasks import TaskUpdate
from core.intent_ir import IntentIR, IntentOperation
import test_trips as fixtures
from test_trips import SPEC, model_response, plan_value


class TripToolContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TripTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_creation_and_reminders_commit_only_after_explicit_confirmation(self):
        c = self.fixture
        preview = c.create(with_reminder=True, confirm=False)
        self.assertIn('行程创建预览', preview)
        self.assertEqual(c.store.trips.list_for_owner(c.event('', 'read')), ())
        self.assertEqual(c.reminders(), ())
        self.assertIn('已创建行程', c.handle('确认行程修改', 'confirm'))
        self.assertEqual(c.trip()['version'], 1)
        self.assertEqual(len(c.reminders()), 1)
        c.handle('确认行程修改', 'duplicate-confirm')
        self.assertEqual(c.trip()['version'], 1)
        self.assertEqual(len(c.reminders()), 1)

    def test_expired_new_draft_can_be_edited_by_id_or_natural_context_without_saving(self):
        from datetime import timedelta
        from tools.trip.tool import trip_context
        c=self.fixture;c.create(confirm=False)
        first=c.store.trips.current_drafts(c.event('','read'))[0]
        trip_id=first.slots['trip_id'];c.now+=timedelta(hours=2)
        text=f'请把 {trip_id} 行程的去程改为：二等座'
        event=c.event(text,'expired-edit')
        changes={'intercity':{'value':{'outbound':{'seat_class':{'value':'second_class','source':'二等座'}}},'source':'二等座'}}
        ir=IntentIR(1,'update',(IntentOperation('trip','update_constraints',{'changes':changes}),),source_spans=('二等座',))
        trips,context=trip_context(c.store,event)
        self.assertEqual(context['current_trip_index'],1)
        with patch.object(c.service,'_generate',side_effect=lambda event,spec,plan,*a,**kw:(kw['previous_sources'],plan)),\
             patch.object(c.planner,'add_route_evidence'):
            reply=c.service.update_intent(event,c.store.delivery.begin_event(event.event_key),ir,trips,context,None,text)
        self.assertIn('行程创建预览',reply)
        self.assertEqual(c.store.trips.list_for_owner(event),())
        current=c.store.trips.current_drafts(event)[0]
        self.assertNotEqual(first.task_id,current.task_id)
        self.assertEqual(current.slots['candidate_trip']['spec']['intercity_request']['outbound']['seat_class'],'second_class')
        self.assertEqual(current.slots['candidate_trip']['spec']['must_visit'],['湖北省博物馆'])
        self.assertEqual(c.store.tasks.recent('onebot','onebot:group','owner',now=c.now)[0].task_id,current.task_id)

    def test_cancel_creation_draft_cannot_be_confirmed_or_create_reminders(self):
        c = self.fixture
        c.create(with_reminder=True, confirm=False)
        self.assertIn('已取消行程创建草稿', c.handle('取消行程', 'cancel'))
        c.handle('确认行程修改', 'confirm')
        self.assertEqual(c.store.trips.list_for_owner(c.event('', 'read')), ())
        self.assertEqual(c.reminders(), ())

    def test_expired_creation_can_be_cancelled_without_restoring_its_confirmation(self):
        from datetime import timedelta
        c=self.fixture;c.create(confirm=False);c.now+=timedelta(hours=2)
        self.assertIn('已取消行程创建草稿',c.handle('取消行程','cancel-expired'))
        self.assertEqual(c.store.trips.current_drafts(c.event('','read')),())
        c.handle('确认行程修改','confirm-old')
        self.assertEqual(c.store.trips.list_for_owner(c.event('','read')),())

    def test_explicit_draft_id_is_not_limited_to_the_recent_candidate_page(self):
        import json
        from tools.trip.tool import trip_context
        c=self.fixture;c.create(confirm=False)
        original=c.store.trips.current_drafts(c.event('','read'))[0]
        with c.store._connect() as db:
            row=dict(db.execute('SELECT * FROM travel_tasks WHERE task_id=?',(original.task_id,)).fetchone())
            for i in range(101):
                other=dict(row);slots=json.loads(other['slots_json']);trip_id='TR-'+format(i,'012x')
                slots['trip_id']=trip_id;slots['candidate_trip']['trip_id']=trip_id
                other.update(task_id='T-page-'+str(i),source_event_id='page-'+str(i),slots_json=json.dumps(slots))
                db.execute('INSERT INTO travel_tasks ('+','.join(other)+') VALUES ('+','.join('?' for _ in other)+')',tuple(other.values()))
        event=c.event('修改行程 '+original.slots['trip_id'],'target-old')
        trips,context=trip_context(c.store,event)
        self.assertEqual(len(trips),1)
        self.assertEqual(trips[0]['trip_id'],original.slots['trip_id'])
        self.assertEqual(context['current_trip_index'],1)

    def test_foreign_owner_cannot_confirm_creation(self):
        c = self.fixture
        c.create(confirm=False)
        c.handle('确认行程修改', 'foreign-confirm', owner='other')
        self.assertEqual(c.store.trips.list_for_owner(c.event('', 'read')), ())
        self.assertIn('已创建行程', c.handle('确认行程修改', 'confirm'))

    def test_multiple_creation_drafts_require_a_bound_target(self):
        c = self.fixture
        c.create(confirm=False)
        first = next(t.slots['trip_id'] for t in c.store.tasks.recent('onebot','onebot:group','owner')
                     if t.slots.get('new_trip_pending'))
        c.client.completions.responses = [model_response(SPEC),model_response(plan_value())]
        c.handle('帮我规划10月1日开始的武汉三天行程，带老人，必去湖北省博物馆','second')
        c.handle('确认行程修改','ambiguous-confirm')
        self.assertEqual(c.store.trips.list_for_owner(c.event('', 'read')), ())
        c.handle('确认行程修改 '+first,'bound-confirm')
        self.assertEqual(c.trip()['trip_id'],first)

    def test_typed_creation_is_not_reclassified_as_edit_by_adjustment_word(self):
        c=self.fixture
        c.create()
        original=c.trip()
        c.client.completions.responses=[model_response(SPEC),model_response(plan_value())]
        event=c.event('规划10月1日开始的武汉三天行程，带老人，必去湖北省博物馆，重新调整节奏','new-trip')
        ir=IntentIR(1,'create',(IntentOperation('trip','create',{'request_text':event.content}),),source_spans=(event.content,))
        c.service.apply_text_intent(event,c.store.delivery.begin_event(event.event_key),ir)
        self.assertEqual(c.trip()['trip_id'],original['trip_id'])
        self.assertEqual(c.trip()['version'],1)
        c.handle('确认行程修改','new-confirm')
        self.assertEqual(len(c.store.trips.list_for_owner(event)),2)

    def test_creation_transaction_rolls_back_trip_when_reminder_write_fails(self):
        c = self.fixture
        c.create(with_reminder=True, confirm=False)
        with patch.object(c.store.reminders, 'apply_change', side_effect=ValueError('conflict')):
            with self.assertRaisesRegex(ValueError, 'conflict'):
                c.handle('确认行程修改', 'failed-confirm')
        self.assertEqual(c.store.trips.list_for_owner(c.event('', 'read')), ())
        self.assertEqual(c.reminders(), ())
        c.handle('确认行程修改', 'retry-confirm')
        self.assertEqual(c.trip()['version'], 1)

    def test_pending_creation_date_edit_stays_unsaved_until_confirmation(self):
        c = self.fixture
        c.client.completions.responses = [model_response({**SPEC,'start_date_text':'国庆'}), model_response(plan_value())]
        c.handle('帮我规划国庆武汉三天行程，带老人，必去湖北省博物馆', 'create')
        preview = c.handle('10月2日', 'date')
        self.assertIn('2030-10-02', preview)
        self.assertEqual(c.store.trips.list_for_owner(c.event('', 'read')), ())
        c.handle('确认行程修改', 'confirm')
        self.assertEqual(c.trip()['spec']['start_date'], '2030-10-02')

    def test_structured_removal_preserves_original_event_and_pending_constraints(self):
        c = self.fixture
        c.create()
        current = c.trip()
        candidate = deepcopy(current)
        candidate['spec']['budget_text'] = '500元'
        candidate['version'] = 2
        e = c.event('预算500元', 'budget')
        c.service._preview_candidate(e,c.store.delivery.begin_event(e.event_key),candidate,current=current)
        event = c.event('10月1日不去湖北省博物馆', 'remove')
        ir = IntentIR(1,'update',(IntentOperation('trip','remove_activity',
            {'activity':{'name':'湖北省博物馆','date_text':'10月1日'}}),))
        with patch.object(c.service,'handle',side_effect=AssertionError('must not reparse language')):
            reply = c.service.handle_intent(event,c.store.delivery.begin_event(event.event_key),ir)
        self.assertIn('500元', reply)
        self.assertEqual(c.trip()['version'], 1)
        c.handle('确认行程修改', 'confirm')
        self.assertEqual(c.trip()['spec']['budget_text'], '500元')
        self.assertNotIn('h',[a['poi_id'] for a in c.trip()['plan']['days'][0]['activities']])

    def test_date_edit_preserves_pending_budget(self):
        c = self.fixture
        c.create()
        current = c.trip()
        candidate = deepcopy(current)
        candidate['spec']['budget_text'] = '500元'
        candidate['version'] = 2
        e = c.event('预算500元','budget')
        c.service._preview_candidate(e,c.store.delivery.begin_event(e.event_key),candidate,current=current)
        c.handle('把行程改到10月2日开始','date')
        c.handle('确认行程修改','confirm')
        self.assertEqual(c.trip()['spec']['budget_text'],'500元')


    def test_consumed_semantic_task_does_not_block_draft_continuation(self):
        c = self.fixture
        c.create()
        e = c.event('修改预算','semantic')
        c.store.tasks.prepare_result(e,c.store.delivery.begin_event(e.event_key),
            TaskUpdate('semantic','collecting',e.content,{},('budget',)),'补充预算')
        task = next(t for t in c.store.tasks.recent('onebot','onebot:group','owner') if t.task_type=='semantic')
        current = c.trip()
        candidate = deepcopy(current)
        candidate['version'] = 2
        e = c.event('预算500元','budget')
        c.service._preview_candidate(e,c.store.delivery.begin_event(e.event_key),candidate,current=current,
            slots={'semantic_task':{'task_id':task.task_id,'version':task.version}})
        reply = c.handle('第一天不去湖北省博物馆','remove')
        self.assertIn('尚未保存',reply)

