from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from tools.trip.constraints import apply_constraints, facts
from tools.trip.planner import TripPlanner, planning_context
from tools.trip.presentation import TripAnswerWriter, validate_answer
from tools.trip.tool import TripTool
from test_rail_window_repair import FIXTURE, NOW
from test_trip_presentation import ANSWER
from test_trips import model_response
from test_travel_agent import FakeClient
from test_draft_actions import WorkflowHarness


def field(value,source):return {'value':value,'source':source}


class ArrivalConsistencyTests(unittest.TestCase):
    def trip(self):
        return {'trip_id':'TR-arrival','version':1,'title':'到达时段测试','spec':deepcopy(FIXTURE['spec']),
                'plan':deepcopy(FIXTURE['plan']),'sources':deepcopy(FIXTURE['places'])}

    def test_new_city_scoped_arrival_replaces_previous_reference_with_current_evidence(self):
        spec=self.trip()['spec'];before=deepcopy(spec)
        text='往返交通，上午到南京'
        patch={'scope':field('round_trip','往返交通'),'outbound':{'arrival_window':field('上午','上午到南京'),
               'destination':field('南京','南京'),'destination_scope':field('city','南京')}}
        result,_=apply_constraints(spec,{'intercity':field(patch,text)},text)
        self.assertEqual(result['city_windows']['arrival']['time_text'],'上午')
        self.assertEqual(result['city_windows']['arrival']['source'],'上午到南京')
        self.assertEqual(spec,before)

    def test_old_saved_facts_use_current_arrival_without_mutating_saved_state(self):
        spec=self.trip()['spec'];before=deepcopy(spec)
        row=next(r for r in facts(spec) if r['field']=='city_windows.arrival')
        self.assertEqual(row['value'],'第1天上午')
        self.assertIn('上午',row['source'])
        self.assertEqual(spec,before)

    def test_station_arrival_does_not_replace_city_arrival(self):
        spec=self.trip()['spec'];leg=spec['intercity_request']['outbound']
        leg.update(destination='南京南',destination_scope='station')
        row=next(r for r in facts(spec) if r['field']=='city_windows.arrival')
        self.assertEqual(row['value'],'第1天早上')
        self.assertIn('市区',row['label'])

    def test_query_permission_does_not_discard_an_explicit_arrival_requirement(self):
        spec=self.trip()['spec'];spec['intercity_request']['scope']='none'
        row=next(r for r in facts(spec) if r['field']=='city_windows.arrival')
        self.assertEqual(row['value'],'第1天上午')
        self.assertEqual(spec['intercity_request']['scope'],'none')

    def test_unknown_destination_cannot_establish_a_shared_arrival_event(self):
        spec=self.trip()['spec'];spec['destination']=''
        spec['intercity_request']['outbound']['destination']=''
        row=next(r for r in facts(spec) if r['field']=='city_windows.arrival')
        self.assertEqual(row['value'],'第1天早上')

    def test_independent_urban_arrival_remains_separate_from_rail_arrival(self):
        spec=self.trip()['spec'];text='第一天中午到市区'
        result,_=apply_constraints(spec,{'city_windows':field({'arrival':{
            'day_index':1,'time_text':'中午','source':text,'independent':True}},text)},text)
        self.assertEqual(result['city_windows']['arrival']['time_text'],'中午')
        self.assertEqual(result['intercity_request']['outbound']['arrival_window'],'上午')
        self.assertIn('市区',next(r['label'] for r in facts(result) if r['field']=='city_windows.arrival'))

    def test_latest_city_edit_updates_the_same_existing_arrival_event(self):
        spec=self.trip()['spec'];text='第一天下午到南京'
        result,_=apply_constraints(spec,{'city_windows':field({'arrival':{
            'day_index':1,'time_text':'下午','source':text}},text)},text)
        self.assertEqual(result['intercity_request']['outbound']['arrival_window'],'下午')
        self.assertEqual(result['city_windows']['arrival']['time_text'],'下午')
        self.assertEqual(result['intercity_request']['outbound']['field_sources']['arrival_window'],text)

    def test_same_turn_inconsistent_claims_require_distinguishing_the_events(self):
        text='上午到南京，第一天下午到市区'
        changes={'intercity':field({'outbound':{'arrival_window':field('上午','上午到南京')}},text),
                 'city_windows':field({'arrival':{'day_index':1,'time_text':'下午','source':'第一天下午到市区'}},text)}
        with self.assertRaises(ValueError):apply_constraints(self.trip()['spec'],changes,text)

    def test_new_arrival_bounds_are_used_before_a_train_is_selected(self):
        spec=self.trip()['spec'];leg=spec['intercity_request']['outbound']
        leg['arrival_window']='下午';leg['field_sources']['arrival_window']='下午到南京'
        TripPlanner.intercity_legs(spec,NOW)
        self.assertEqual(spec['city_windows']['arrival']['time_text'],'下午')
        window=TripPlanner.transport_windows(spec,{})['1']
        self.assertNotIn('上午',window['allowed_periods'])
        self.assertEqual(window['earliest'][11:16],'18:00')

    def test_old_narrative_is_not_reused_after_arrival_projection_changes(self):
        trip=self.trip();trip['answer']=validate_answer({**ANSWER,'overview':'早上抵达后安排参观。'},trip)
        before=deepcopy(trip)
        text=TripTool.format_trip(trip)
        self.assertIn('第1天上午',text)
        self.assertNotIn('早上抵达',text)
        self.assertEqual(trip,before)

    def test_writer_receives_only_the_effective_arrival_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'test.db');client=FakeClient([model_response(ANSWER)])
            event=ChatEvent('web','group','arrival','scope','owner','更新交通')
            store.delivery.begin_event(event.event_key);trip=self.trip();before=deepcopy(trip)
            TripAnswerWriter(store,client,'test').generate(event,trip,[])
            payload=json.loads(client.completions.requests[0]['messages'][-1]['content'])
            self.assertEqual(payload['requirements']['city_windows']['arrival']['time_text'],'上午')
            self.assertEqual(trip,before)

    def test_planning_input_uses_the_same_effective_arrival_without_mutation(self):
        trip=self.trip();before=deepcopy(trip)
        payload=planning_context(trip['spec'],trip['sources'])
        self.assertEqual(payload['requirements']['city_windows']['arrival']['time_text'],'上午')
        self.assertEqual(trip,before)


class ArrivalSidebarTests(WorkflowHarness,unittest.TestCase):
    def test_saved_sidebar_uses_effective_arrival_without_rewriting_the_trip(self):
        draft=self.create();self.save(draft)
        with self.web.store._connect() as db:
            row=db.execute('SELECT trip_id,spec_json FROM trips').fetchone()
            spec=json.loads(row['spec_json'])
            spec['city_windows']={'arrival':{'day_index':1,'time_text':'早上','source':'第一天早上到南京市区'}}
            spec['intercity_request']={'scope':'round_trip','outbound':{'destination':spec['destination'],
                'destination_scope':'city','arrival_window':'上午','resolved_date':spec['start_date'],
                'field_sources':{'arrival_window':'上午到南京'}}}
            raw=json.dumps(spec,ensure_ascii=False)
            db.execute('UPDATE trips SET spec_json=? WHERE trip_id=?',(raw,row['trip_id']))
        visible=self.context()['trips'][0]
        self.assertEqual(visible['spec']['city_windows']['arrival']['time_text'],'上午')
        self.assertEqual(next(f['value'] for f in visible['facts'] if f['field']=='city_windows.arrival'),'第1天上午')
        with self.web.store._connect() as db:
            self.assertEqual(db.execute('SELECT spec_json FROM trips WHERE trip_id=?',(row['trip_id'],)).fetchone()[0],raw)
