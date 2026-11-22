from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.trip.presentation import validate_answer, render_answer, TripAnswerWriter
from tools.trip.tool import TripTool
from test_trips import SPEC, PLACES, model_response
from test_place_recommendations import PLAN
from test_travel_agent import FakeClient
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore


ANSWER={'overview':'顺着游览区域安排，减少来回折返，把文化参观与户外散步穿插起来。',
        'change_summary':'住宿预算已更新，原有景点与交通保留。','days':[{'day_index':1,'text':'先慢慢走进城市的历史，再留一些放松的空间。'}],
        'stay':'围绕主要游览区域选择住宿，抵达后先安顿行李。','meals':[]}


class TripPresentationTests(unittest.TestCase):
    def test_budget_default_disclosure_survives_both_answer_renderers_and_fact_cards(self):
        from tools.trip.constraints import apply_constraints, facts
        trip=self.trip()
        for day in trip['plan']['days']:day['date']='2030-10-01'
        trip['spec'],_=apply_constraints(trip['spec'],{'budget':{'scope':'accommodation','value':'300-400','source':'300-400'}},'酒店300-400')
        original=deepcopy(trip)
        for use_narrative in (False,True):
            if use_narrative:trip['answer']=validate_answer(ANSWER,trip)
            for preview in (False,True):
                text=TripTool.format_trip(trip,preview=preview)
                for detail in ('300-400','人民币每间每晚','默认币种：人民币','默认计价：每间每晚'):
                    self.assertIn(detail,text)
        row=next(row for row in facts(trip['spec']) if row['field']=='budgets.accommodation')
        self.assertIn('默认币种：人民币',row['value'])
        self.assertIn('默认计价：每间每晚',row['value'])
        self.assertEqual(trip['spec'],original['spec'])

    def trip(self):
        return {'trip_id':'TR-123456789abc','title':'测试行程','version':1,'spec':{**SPEC,'day_count':2},
                'plan':deepcopy(PLAN),'sources':{**PLACES,**{pid:{'name':pid} for pid in ('a','b','c','d')}}}

    def test_narrative_cannot_introduce_prices_hours_or_invalid_day(self):
        trip=self.trip()
        for patch in ({'overview':'门票免费，无需预约'},{'stay':'酒店200元'},
                      {'days':[{'day_index':3,'text':'散步。'}]},
                      {'meals':[{'day_index':9,'meal':'午餐','text':'清淡一点。'}]}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):validate_answer({**ANSWER,**patch},trip)

    def test_update_is_concise_and_full_view_still_has_canonical_days(self):
        trip=self.trip();trip['answer']=validate_answer(ANSWER,trip)
        update,_=render_answer(trip,TripTool.format_intercity,preview=True,mode='update')
        full,sections=render_answer(trip,TripTool.format_intercity,preview=True)
        self.assertNotIn('### 第',update)
        self.assertIn('住宿预算已更新',update)
        self.assertIn('### 第1天',full)
        self.assertIn('本轮尚未取得该景点介绍',full)
        self.assertEqual('\n'.join(s['text'] for s in sections if s['kind']=='text'),full)

    def test_writer_has_one_checkpoint_and_does_not_query_suppliers(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'writer.db')
            event=ChatEvent('web','group','answer','conversation','owner','规划行程')
            store.delivery.begin_event(event.event_key)
            client=FakeClient([model_response(ANSWER)])
            writer=TripAnswerWriter(store,client,'test')
            trip=self.trip();before=deepcopy(trip)
            first=writer.generate(event,trip,[])
            self.assertIsNotNone(first)
            self.assertEqual(writer.generate(event,trip,[]),first)
            self.assertEqual(len(client.completions.requests),1)
            self.assertEqual(trip,before)

    def test_bad_generation_falls_back_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'writer.db')
            event=ChatEvent('web','group','bad-answer','conversation','owner','规划行程')
            store.delivery.begin_event(event.event_key)
            client=FakeClient([model_response({'overview':'这里门票20元'})])
            writer=TripAnswerWriter(store,client,'test')
            self.assertIsNone(writer.generate(event,self.trip(),[]))
            self.assertIsNone(writer.generate(event,self.trip(),[]))
            self.assertEqual(len(client.completions.requests),1)
