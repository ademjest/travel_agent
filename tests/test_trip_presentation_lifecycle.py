from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from infrastructure.memory_store import MemoryStore
from test_draft_actions import WorkflowHarness
from test_travel_agent import FakeClient
from test_trips import model_response
from test_trip_presentation import ANSWER
from tools.trip.planner import TripPlanner
from tools.trip.presentation import TripAnswerWriter
from tools.trip.tool import TripTool


class PreferenceBoundaryTests(unittest.TestCase):
    def test_extract_separates_workflow_from_positive_and_negative_travel_constraints(self):
        preferences=['两位成年人','节奏轻松一些','不吃辣','不去寺庙',
                     '保存体力，少爬楼梯','先给我草案，不要直接保存',
                     '尽量步行，先不要再调整方案']
        text='南京三天；'+'；'.join(preferences)
        planner=TripPlanner(FakeClient([model_response({'destination':'南京','duration_text':'三天',
            'preferences':preferences})]),'fixture',None)
        spec=planner.extract(text,{},datetime(2030,9,30,tzinfo=timezone.utc))
        self.assertEqual(spec['preferences'],['两位成年人','节奏轻松一些','不吃辣','不去寺庙',
                                              '保存体力，少爬楼梯','尽量步行'])


class PresentationLifecycleTests(WorkflowHarness,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.writer_client=FakeClient([model_response(ANSWER)])
        self.tool.answer_writer=TripAnswerWriter(self.web.store,self.writer_client,'fixture')

    def test_save_reload_and_full_view_reuse_exact_version_narrative_without_model_calls(self):
        draft=self.create();candidate=self.candidate();self.save(draft)
        store=MemoryStore(self.web.store.database_path)
        event=self.web.event('查看行程')
        loaded=store.trips.get(event,candidate['trip_id'])
        self.assertEqual(loaded.get('answer'),candidate['answer'])
        self.assertEqual(loaded.get('answer_basis'),candidate['answer_basis'])
        self.assertIn(ANSWER['overview'],TripTool.format_trip(loaded))
        self.assertEqual(store.trips.list_for_owner(event)[0]['answer'],candidate['answer'])
        with store._connect() as db:
            snapshot=json.loads(db.execute('SELECT snapshot_json FROM trip_versions WHERE trip_id=? AND version=1',
                                          (candidate['trip_id'],)).fetchone()[0])
        self.assertEqual(snapshot.get('answer'),candidate['answer'])
        self.assertEqual(len(self.writer_client.completions.requests),1)

    def test_legacy_exact_version_receipt_restores_answer_without_database_mutation(self):
        draft=self.create();candidate=self.candidate();self.save(draft)
        with self.web.store._connect() as db:
            snapshot=json.loads(db.execute('SELECT snapshot_json FROM trip_versions').fetchone()[0])
            snapshot.pop('answer',None);snapshot.pop('answer_basis',None)
            raw=json.dumps(snapshot,ensure_ascii=False)
            db.execute('UPDATE trip_versions SET snapshot_json=?',(raw,))
        loaded=self.web.store.trips.get(self.web.event(''),candidate['trip_id'])
        self.assertEqual(loaded.get('answer'),candidate['answer'])
        with self.web.store._connect() as db:
            self.assertEqual(db.execute('SELECT snapshot_json FROM trip_versions').fetchone()[0],raw)
        self.assertEqual(len(self.writer_client.completions.requests),1)

    def test_changed_facts_do_not_restore_stale_answer_from_snapshot_or_receipt(self):
        draft=self.create();candidate=self.candidate();self.save(draft)
        with self.web.store._connect() as db:
            spec=json.loads(db.execute('SELECT spec_json FROM trips').fetchone()[0])
            spec['preferences'].append('不去室外')
            db.execute('UPDATE trips SET spec_json=?',(json.dumps(spec),))
        loaded=self.web.store.trips.get(self.web.event(''),candidate['trip_id'])
        self.assertNotIn('answer',loaded)
        self.assertEqual(len(self.writer_client.completions.requests),1)

    def test_old_template_controls_are_not_rendered_or_mutated(self):
        self.create();trip=self.candidate();trip.pop('answer',None)
        trip['spec']['preferences']=['不吃辣','先给我草案，不要直接保存']
        for day in trip['plan']['days']:
            for activity in day['activities']:
                activity['description']={'intro':{'text':'保留景点介绍','source_ids':[1]},
                    'why_visit':'结合你提出的“不吃辣；先给我草案，不要直接保存”，将这里作为文化参观的一站，停留可按体力调整。'}
        before=deepcopy(trip);text=TripTool.format_trip(trip)
        self.assertNotIn('不要直接保存',text)
        self.assertNotIn('结合你提出的',text)
        self.assertIn('不吃辣',text);self.assertIn('保留景点介绍',text)
        self.assertEqual(trip,before)

    def test_writer_receives_only_travel_preferences(self):
        self.create();trip=self.candidate()
        trip['spec']['preferences']=['节奏轻松一些','先给我草案，不要直接保存']
        self.writer_client.completions.responses.append(model_response(ANSWER))
        event=self.web.event('先给预览','writer-clean')
        self.web.store.delivery.begin_event(event.event_key)
        self.tool.answer_writer.generate(event,trip,[])
        payload=json.loads(self.writer_client.completions.requests[-1]['messages'][1]['content'])
        self.assertEqual(payload['requirements']['preferences'],['节奏轻松一些'])

    def test_sidebar_legacy_constraints_are_projected_without_rewriting_saved_preferences(self):
        draft=self.create();saved=self.save(draft)
        with self.web.store._connect() as db:
            spec=json.loads(db.execute('SELECT spec_json FROM trips').fetchone()[0])
            spec['preferences']=['不吃辣','先给我草案，不要直接保存']
            raw=json.dumps(spec,ensure_ascii=False)
            db.execute('UPDATE trips SET spec_json=?',(raw,))
        visible=self.context()['trips'][0]
        self.assertEqual(visible['spec']['preferences'],['不吃辣'])
        self.assertNotIn('不要直接保存',json.dumps(visible['facts'],ensure_ascii=False))
        with self.web.store._connect() as db:
            self.assertEqual(db.execute('SELECT spec_json FROM trips').fetchone()[0],raw)

