import json
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import Mock

from agents.intent_compiler import IntentCompiler
from core.chat_transport import ChatEvent
from services.semantic_task_service import SemanticTaskService, semantic_candidate
from app.group_trigger_policy import GroupTriggerPolicy
from infrastructure.memory_store import MemoryStore
from services.trip_service import TripService
from core.tasks import TaskUpdate


class FakeClient:
    def __init__(self, value):
        self.value = value
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.value, ensure_ascii=False)))])


def compiler(value):
    return IntentCompiler(FakeClient(value), 'test')


def event(text, event_id='event'):
    return ChatEvent('onebot', 'group', event_id, 'group', 'owner', text,
                     occurred_at=datetime(2030, 9, 9, 2, tzinfo=timezone.utc))


class SemanticTaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.trip = {'trip_id': 'TR-real', 'plan': {'days': [
            {'day_index': 1, 'date': '2030-10-02', 'activities': [{'poi_id': 'museum'}]},
        ]}, 'sources': {'museum': {'name': '湖北省博物馆'}}}
        self.store = SimpleNamespace(trips=SimpleNamespace(list_for_owner=Mock(return_value=(self.trip,))))
        self.trip_service = Mock()
        self.reminder_service = Mock()

    def test_fixed_commands_use_fast_path(self):
        self.assertFalse(semantic_candidate('查看行程'))
        self.assertFalse(semantic_candidate('查看我的提醒'))
        self.assertTrue(semantic_candidate('10月2日不去湖北省博物馆了'))

    def test_missing_place_is_persisted_and_short_answer_continues(self):
        first = {'schema_version': 1, 'action': 'update', 'operations': [
            {'domain': 'trip', 'operation': 'remove_activity', 'activity': {'date_text': '10月2日'}}],
            'missing_fields': ['地点'], 'requires_confirmation': True, 'source_spans': ['10月2日'], 'confidence': .8}
        second = {'schema_version': 1, 'action': 'update', 'operations': [
            {'domain': 'trip', 'operation': 'remove_activity', 'activity': {'name': '湖北省博物馆', 'date_text': '10月2日'}}],
            'missing_fields': [], 'requires_confirmation': True, 'source_spans': ['10月2日', '湖北省博物馆'], 'confidence': .9}
        class QueueCompiler:
            def __init__(self): self.values = [first, second]
            def try_compile(self, text, **kwargs): return __import__('core.intent_ir', fromlist=['parse_intent_ir']).parse_intent_ir(self.values.pop(0), text)
        class Tasks:
            def __init__(self): self.pending = None
            def recent(self, *args, **kwargs): return (self.pending,) if self.pending else ()
            def prepare_result(self, event_value, claim, update, reply):
                self.pending = SimpleNamespace(task_type='semantic', status='collecting', initial_request=update.initial_request,
                    slots=update.slots, missing_slots=update.missing_slots, task_id='semantic-task', version=1)
        tasks = Tasks()
        store = SimpleNamespace(trips=self.store.trips, tasks=tasks)
        trip_service = Mock()
        trip_service.handle.return_value = '预览'
        service = SemanticTaskService(store, QueueCompiler(), trip_service, mode='preview')
        first_event = event('10月2日不去那个博物馆了')
        self.assertEqual(service.handle(first_event, object()), '请说明要从行程中删除的具体地点。')
        self.assertIsNotNone(tasks.pending)
        self.assertEqual(service.handle(event('湖北省博物馆', 'answer'), object()), '预览')
        self.assertEqual(trip_service.handle.call_args.args[0].content, '第1天不去湖北省博物馆')

    def test_model_omitted_place_is_recovered_from_owned_trip_and_original_text(self):
        value = {'schema_version': 1, 'action': 'update', 'operations': [
            {'domain': 'trip', 'operation': 'remove_activity', 'activity': {'date_text': '10月2日'}}],
            'missing_fields': ['地点'], 'requires_confirmation': True, 'source_spans': ['10月2日'], 'confidence': .8}
        self.trip_service.handle.return_value = '预览'
        service = SemanticTaskService(self.store, compiler(value), self.trip_service, mode='preview')
        self.assertEqual(service.handle(event('10月2日不去湖北省博物馆了'), object()), '预览')
        self.assertEqual(self.trip_service.handle.call_args.args[0].content, '第1天不去湖北省博物馆')

    def test_clear_owned_trip_edit_recovers_when_model_misclassifies_as_places(self):
        value = {'schema_version': 1, 'action': 'read', 'operations': [
            {'domain': 'places', 'operation': 'search', 'keywords': '湖北省博物馆'}],
            'missing_fields': [], 'requires_confirmation': False, 'source_spans': ['湖北省博物馆'], 'confidence': .7}
        self.trip_service.handle.return_value = '预览'
        service = SemanticTaskService(self.store, compiler(value), self.trip_service, mode='execute')
        self.assertEqual(service.handle(event('10月2日不去湖北省博物馆了'), object()), '预览')
        self.assertEqual(self.trip_service.handle.call_args.args[0].content, '第1天不去湖北省博物馆')

    def test_pending_semantic_task_allows_unmentioned_short_answer(self):
        class Tasks:
            def recent(self, *args, **kwargs):
                return (SimpleNamespace(task_type='semantic', status='collecting', missing_slots=('地点',)),)
            def has_history(self, *args): return True
        policy = GroupTriggerPolicy(SimpleNamespace(tasks=Tasks()))
        import asyncio
        self.assertTrue(asyncio.run(policy.should_handle(event('湖北省博物馆'))))

    def test_shadow_mode_never_reaches_domain_service(self):
        value = {'schema_version': 1, 'action': 'update', 'operations': [
            {'domain': 'trip', 'operation': 'remove_activity', 'activity': {'name': '湖北省博物馆', 'date_text': '10月2日'}}],
            'missing_fields': [], 'requires_confirmation': True, 'source_spans': ['湖北省博物馆'], 'confidence': .9}
        service = SemanticTaskService(self.store, compiler(value), self.trip_service, self.reminder_service, mode='shadow')
        self.assertIsNone(service.handle(event('10月2日不去湖北省博物馆了'), object()))
        self.trip_service.handle.assert_not_called()

    def test_date_based_removal_is_translated_to_existing_trip_flow(self):
        value = {'schema_version': 1, 'action': 'update', 'operations': [
            {'domain': 'trip', 'operation': 'remove_activity', 'activity': {'name': '湖北省博物馆', 'date_text': '10月2日'}}],
            'missing_fields': [], 'requires_confirmation': True, 'source_spans': ['10月2日', '湖北省博物馆'], 'confidence': .9}
        self.trip_service.handle.return_value = '预览'
        service = SemanticTaskService(self.store, compiler(value), self.trip_service, mode='preview')
        self.assertEqual(service.handle(event('10月2日不去湖北省博物馆了'), object()), '预览')
        canonical_event = self.trip_service.handle.call_args.args[0]
        self.assertEqual(canonical_event.content, '第1天不去湖北省博物馆')
        self.assertTrue(self.trip_service.handle.call_args.kwargs['force_confirmation'])

    def test_natural_confirmation_reuses_pending_trip_preview(self):
        value = {'schema_version': 1, 'action': 'confirm', 'operations': [
            {'domain': 'trip', 'operation': 'confirm_pending_operation'}], 'missing_fields': [],
            'requires_confirmation': False, 'source_spans': [], 'confidence': .9}
        self.trip_service.handle.return_value = '已变更'
        service = SemanticTaskService(self.store, compiler(value), self.trip_service, mode='execute')
        self.assertEqual(service.handle(event('确认取消调整，请变更行程'), object()), '已变更')
        self.assertEqual(self.trip_service.handle.call_args.args[0].content, '确认行程修改')

    def test_semantic_reminder_uses_deterministic_service_after_validation(self):
        value = {'schema_version': 1, 'action': 'create', 'operations': [
            {'domain': 'reminder', 'operation': 'create', 'title': '登录王者做任务', 'trigger': {'text': '五分钟后'}}],
            'missing_fields': [], 'requires_confirmation': False, 'source_spans': ['五分钟后', '登录王者做任务'], 'confidence': .9}
        self.reminder_service.handle_intent.return_value = '已设置'
        service = SemanticTaskService(self.store, compiler(value), self.trip_service, self.reminder_service, mode='execute')
        self.assertEqual(service.handle(event('五分钟后提醒我登录王者做任务'), object()), '已设置')
        self.assertEqual(self.reminder_service.handle_intent.call_args.args[0].content, '五分钟后提醒我登录王者做任务')
        self.assertEqual(self.reminder_service.handle_intent.call_args.kwargs['time_text'], '五分钟后')

    def test_natural_remove_and_confirmation_commit_trip_and_linked_reminder_together(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(f'{folder}/trip.db')
            class Planner:
                @staticmethod
                def validate_plan(plan, spec, sources): return plan
                @staticmethod
                def add_route_evidence(plan, spec, sources, previous=None): return plan
            trip_service = TripService(store, Planner())
            create_event = ChatEvent('onebot', 'group', 'trip-create', 'group', 'owner', 'create')
            trip = {'trip_id': 'TR-semantic', 'title': '武汉行程', 'status': 'active',
                'spec': {'destination': '武汉', 'day_count': 1, 'reminder_entities': ['湖北省博物馆'], 'must_visit': ['湖北省博物馆']},
                'plan': {'days': [{'day_index': 1, 'date': '2030-10-02', 'activities': [{'poi_id': 'museum', 'period': '上午', 'reason': '文化参观'}]}]},
                'sources': {'museum': {'name': '湖北省博物馆', 'address': '测试路', 'type': '博物馆'}}}
            store.trips.commit(create_event, store.begin_event(create_event.event_key),
                TaskUpdate('itinerary', 'completed', 'create', {'trip_id': trip['trip_id']}), 'created', trip=trip)
            reminder_event = ChatEvent('onebot', 'group', 'reminder-create', 'group', 'owner', 'create reminder')
            reminder = {'reminder_id': 'M-linked', 'title': '预约湖北省博物馆（参观日期 2030-10-02）',
                'scheduled_at_utc': '2030-09-27T00:00:00+00:00',
                'source': {'trip_id': trip['trip_id'], 'entity': '湖北省博物馆', 'visit_date': '2030-10-02'}}
            store.reminders.commit(reminder_event, store.begin_event(reminder_event.event_key),
                TaskUpdate('reminder', 'completed', 'create reminder', {}), 'created', action='create', reminder=reminder)
            remove_text = '10月2日不去湖北省博物馆了'
            remove_ir = {'schema_version': 1, 'action': 'update', 'operations': [
                {'domain': 'trip', 'operation': 'remove_activity', 'activity': {'name': '湖北省博物馆', 'date_text': '10月2日'}},
                {'domain': 'reminder', 'operation': 'cancel_linked_reminder', 'entity': '湖北省博物馆'}],
                'missing_fields': [], 'requires_confirmation': True, 'source_spans': ['10月2日', '湖北省博物馆'], 'confidence': .9}
            service = SemanticTaskService(store, compiler(remove_ir), trip_service, mode='execute')
            preview_event = event(remove_text, 'semantic-remove')
            preview = service.handle(preview_event, store.begin_event(preview_event.event_key))
            self.assertIn('尚未保存', preview)
            self.assertEqual(store.trips.get(event('read'), trip['trip_id'])['version'], 1)
            self.assertEqual(store.trips.linked_reminders(event('read'), trip['trip_id'])[0]['status'], 'active')
            repeated = event(remove_text, 'semantic-remove-again')
            SemanticTaskService(store, compiler(remove_ir), trip_service, mode='execute').handle(
                repeated, store.begin_event(repeated.event_key))
            with store._connect() as connection:
                states = connection.execute("SELECT status, count(*) FROM travel_tasks WHERE task_type='itinerary' GROUP BY status").fetchall()
            self.assertEqual(dict(states).get('collecting'), 1)
            confirm_ir = {'schema_version': 1, 'action': 'confirm', 'operations': [
                {'domain': 'trip', 'operation': 'confirm_pending_operation'}], 'missing_fields': [],
                'requires_confirmation': False, 'source_spans': [], 'confidence': .9}
            confirm = event('确认取消调整，请变更行程', 'semantic-confirm')
            result = SemanticTaskService(store, compiler(confirm_ir), trip_service, mode='execute').handle(confirm, store.begin_event(confirm.event_key))
            self.assertIn('已应用', result)
            self.assertEqual(store.trips.get(event('read'), trip['trip_id'])['version'], 2)
            self.assertEqual(store.trips.linked_reminders(event('read'), trip['trip_id'], include_cancelled=True)[0]['status'], 'cancelled')
