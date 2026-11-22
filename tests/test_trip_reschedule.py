"""Whole-trip date edits must update requirements before transport normalizes the calendar."""
from copy import deepcopy
import json
import unittest

from core.tool_result import ToolResult
from tools.reminders.tool import RemindersTool
from tools.trip.planner import TripPlanError
from tools.trip.constraints import reschedule_dates
import test_trips as trip_fixtures


class TripRescheduleTests(unittest.TestCase):
    setUp=trip_fixtures.TripTests.setUp
    event=trip_fixtures.TripTests.event
    handle=trip_fixtures.TripTests.handle
    create=trip_fixtures.TripTests.create
    trip=trip_fixtures.TripTests.trip
    reminders=trip_fixtures.TripTests.reminders

    def seed_transport(self,*,reminder=False):
        self.create(with_reminder=reminder)
        saved=self.trip();spec=saved['spec']
        spec['end_date']='2030-10-03'
        spec['planning_window']={'start':'2030-10-01','end':'2030-10-03','days':3,
            'start_text':'2030-10-01 上午','end_text':'2030-10-03 18点前',
            'start_window':[9,12],'end_window':[0,18],'start_event':'arrival','end_event':'arrival'}
        spec['intercity_request']={'scope':'round_trip','origin':'上海','origin_scope':'city',
            'outbound':{'date_text':'2030年10月1日','resolved_date':'2030-10-01','arrival_window':'上午'},
            'inbound':{'date_text':'2030年10月3日','resolved_date':'2030-10-03','arrival_before':'18点前'}}
        for direction in ('outbound','inbound'):
            spec['intercity_request'][direction].update(train_preference='high_speed',seat_class='second_class',time_reference='arrival')
        spec['budgets']={'accommodation':{'value':'350到450元','unit_text':'人民币每间每晚'},
            'food':{'value':'70以下','unit_text':'人民币人均','meals':{'2:午餐':{'value':'90元以内'},'2:晚餐':{'value':'80元以内'}}}}
        with self.store._connect() as db:
            db.execute('UPDATE trips SET spec_json=? WHERE trip_id=?',(json.dumps(spec,ensure_ascii=False),saved['trip_id']))
        self.calls=[]
        def offline(name,args,context):
            self.calls.append(deepcopy(args))
            return ToolResult('failed',name,error_code='not_available',data='隔离测试无实时车票')
        self.service.rail_executor=offline
        return self.trip()

    def test_reschedule_preserves_requirements_and_previews_before_exact_save(self):
        original=self.seed_transport()
        text='请将整体行程顺延一天，改为10月2日到10月4日。'
        reply=self.handle(text,'shift')
        self.assertIn('尚未保存',reply)
        candidate=self.store.trips.current_drafts(self.event('','read'))[0].slots['candidate_trip']
        self.assertEqual([d['date'] for d in candidate['plan']['days']],['2030-10-02','2030-10-03','2030-10-04'])
        self.assertEqual([q['travel_date'] for q in self.calls],['2030-10-02','2030-10-04'])
        self.assertEqual(candidate['spec']['planning_window']['end_window'],[0,18.0])
        self.assertEqual(candidate['spec']['intercity_request']['inbound']['arrival_before'],'18点前')
        self.assertEqual(candidate['spec']['budgets'],original['spec']['budgets'])
        self.assertEqual([[a['poi_id'] for a in d['activities']] for d in candidate['plan']['days']],
                         [[a['poi_id'] for a in d['activities']] for d in original['plan']['days']])
        self.assertEqual(self.trip(),original)
        self.handle('确认行程修改','save-shift')
        self.assertEqual(self.trip()['version'],2)
        self.assertEqual(self.trip()['spec']['start_date'],'2030-10-02')
        self.assertEqual(len(self.calls),2)

    def test_personal_reminder_unchanged_linked_reminder_changes_only_after_save(self):
        self.seed_transport(reminder=True)
        event=self.event('2030年9月20日晚上8点提醒我核对开放信息','personal')
        RemindersTool(self.store,clock=lambda:self.now).handle_personal(event,self.store.delivery.begin_event(event.event_key))
        before={r['reminder_id']:dict(r) for r in self.reminders()}
        self.assertEqual(len(before),2)
        self.handle('把行程改到10月2日开始','shift-linked')
        self.assertEqual({r['reminder_id']:dict(r) for r in self.reminders()},before)
        self.handle('确认行程提醒','save-linked')
        for row in self.reminders():
            if json.loads(row['source_json']).get('trip_id'):
                self.assertEqual(json.loads(row['source_json'])['visit_date'],'2030-10-02')
                self.assertNotEqual(row['scheduled_at_utc'],before[row['reminder_id']]['scheduled_at_utc'])
            else:self.assertEqual(dict(row),before[row['reminder_id']])

    def test_owned_tickets_are_not_silently_redated(self):
        original=self.seed_transport();spec=original['spec']
        spec['intercity_request']['outbound'].update(mode='owned_ticket',ticket_departure='2030年10月1日8点')
        before=deepcopy(spec)
        with self.assertRaisesRegex(ValueError,'不能随行程自动改签'):
            reschedule_dates(spec,'把行程改到10月2日开始',self.now)
        self.assertEqual(spec,before)

    def test_explicit_end_date_cannot_be_silently_ignored(self):
        original=self.seed_transport()
        with self.assertRaisesRegex(TripPlanError,'改变了旅行天数'):
            self.handle('把行程改为10月2日至10月6日','longer')
        self.assertEqual(self.trip(),original);self.assertEqual(self.calls,[])

    def test_query_restriction_preserves_city_preview_without_using_old_tickets(self):
        original=self.seed_transport()
        self.handle('把行程改到10月2日开始，不联网','offline-shift')
        candidate=self.store.trips.current_drafts(self.event('','read'))[0].slots['candidate_trip']
        self.assertEqual(self.calls,[])
        self.assertEqual(candidate['spec']['start_date'],'2030-10-02')
        self.assertNotIn('selected',candidate['plan']['intercity']['outbound'])
        self.assertEqual(candidate['spec']['budgets'],original['spec']['budgets'])


if __name__=='__main__':unittest.main()
