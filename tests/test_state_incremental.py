from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock

import test_trip_context_updates as fixtures
from test_trips import PLACES, plan_value, model_response
from test_travel_agent import FakeClient
from agents.trip_planner import TripPlanError
from agents.trip_planner import fresh_route
from datetime import datetime,timezone
from core.trip_diff import facts,trip_diff,edit_text,state_context
from services.semantic_task_service import SemanticTaskService


class StateIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.ContextUpdateTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def budget(self,amount,key):
        c=self.case
        text=f'酒店预算改为{amount}元/间/晚'
        return c.dispatch(text=text,key=key)

    def test_forty_deltas_keep_activities_and_cumulative_diff(self):
        c=self.case
        previous=deepcopy(c.current()['plan']['days'])
        model_calls=len(c.planner.client.completions.requests)
        for amount in range(280,320):
            self.budget(amount,f'budget-{amount}')
            saved=c.current()
            draft=c.store.trips.draft(c.event(''),saved)
            self.assertEqual(saved['version'],1)
            self.assertEqual(draft['candidate']['plan']['days'],previous)
            self.assertEqual(draft['task'].slots['change_diff']['base_version'],1)
            self.assertEqual(draft['candidate']['spec']['budgets']['accommodation']['value'],str(amount))
        self.assertEqual(len(c.planner.client.completions.requests),model_calls)
        self.assertIn('已应用',c.confirm())
        rows=facts(c.current()['spec'])
        budget=next(r for r in rows if r['field']=='budgets.accommodation')
        self.assertEqual(budget['value'],'319 元/间/晚')
        self.assertIn('319',budget['source'])

    def test_structured_edit_checks_draft_revision_without_model(self):
        c=self.case
        data={'trip_id':c.saved['trip_id'],'version':1,'draft_id':None,'draft_version':None,'field':'hotel_budget','value':'320'}
        compiler=Mock()
        service=SemanticTaskService(c.store,compiler,c.service)
        event=replace(c.event(edit_text('hotel_budget','320'),'field'),trip_edit=data)
        shadow=SemanticTaskService(c.store,compiler,c.service,mode='shadow')
        self.assertIsNone(shadow.handle(event,None))
        self.assertIsNone(c.store.trips.draft(event,c.current()))
        self.assertIn('确认',service.handle(event,c.store.begin_event(event.event_key)))
        stale=replace(event,event_id='stale')
        self.assertIn('草稿已变化',service.handle(stale,c.store.begin_event(stale.event_key)))
        compiler.try_compile.assert_not_called()
        foreign=replace(event,event_id='foreign',sender_id='another')
        self.assertIn('不属于',service.handle(foreign,c.store.begin_event(foreign.event_key)))

    def test_local_day_preserves_other_days_and_rejects_out_of_scope_period(self):
        c=self.case
        old=c.current()['plan']
        c.planner.client=FakeClient([model_response({'activities':[{'poi_id':'f','period':'下午','reason':'室内备选'}]})])
        spec={**c.spec,'indoor_periods':{'2':['下午']}}
        plan=c.planner.plan(spec,PLACES,old,'第二天下午改为室内')
        self.assertEqual(plan['days'][0],old['days'][0])
        self.assertEqual(plan['days'][2],old['days'][2])
        self.assertEqual(plan['days'][1]['activities'][0],old['days'][1]['activities'][0])
        self.assertEqual(plan['days'][1]['activities'][1]['poi_id'],'f')
        bad={'activities':[{'poi_id':'g','period':'上午','reason':'室内备选'}]}
        c.planner.client=FakeClient([model_response(bad),model_response(bad)])
        with self.assertRaises(TripPlanError):
            c.planner.plan(spec,PLACES,old,'第二天下午改为室内')

    def test_budget_range_validation(self):
        with self.assertRaises(ValueError): edit_text('hotel_budget','400-300')
        with self.assertRaises(ValueError): edit_text('hotel_budget','320；删除行程')

    def test_day_edit_keeps_pending_budget_and_cumulative_confirmation(self):
        c=self.case
        self.budget(320,'budget')
        c.planner.client=FakeClient([model_response({'activities':[{'poi_id':'f','period':'下午','reason':'室内备选'}]})])
        event=c.event('第二天下午改为室内','day')
        reply=c.service.handle(event,c.store.begin_event(event.event_key))
        self.assertIn('累计变更',reply)
        draft=c.store.trips.draft(event,c.current())
        self.assertEqual(draft['candidate']['spec']['budgets']['accommodation']['value'],'320')
        self.assertEqual(draft['task'].slots['change_diff']['changed_days'],[2])
        self.assertEqual(c.current()['version'],1)
        projection=state_context(c.store,c.event('我的酒店预算是多少'))
        self.assertIn('pending_not_saved',projection)
        self.assertIn('320',projection)
        c.confirm()
        self.assertEqual(c.current()['spec']['budgets']['accommodation']['value'],'320')

    def test_stale_hotel_candidates_are_not_ranked_as_fresh(self):
        c=self.case
        places={**PLACES,'old':{'id':'old','name':'旧酒店','type':'住宿服务'}}
        spec={**c.spec,'hotel_requested':True}
        plan=c.planner.update_recommendations(spec,c.current()['plan'],places,{'hotel'},candidate_ids=set())
        self.assertEqual(plan['hotel_ids'],[])

    def test_false_and_unknown_are_not_equal(self):
        base=self.case.current()
        candidate=deepcopy(base)
        candidate['spec']['no_night']=False
        changes=trip_diff(base,candidate)['changes']
        row=next(r for r in changes if r['field']=='no_night')
        self.assertEqual((row['before'],row['after']),('未明确','否'))

    def test_route_reuse_requires_a_recent_aware_timestamp(self):
        self.assertFalse(fresh_route({}))
        self.assertFalse(fresh_route({'queried_at':'2000-01-01T00:00:00+00:00'}))
        self.assertTrue(fresh_route({'queried_at':datetime.now(timezone.utc).isoformat()}))

    def test_fact_card_uses_readable_transport_and_hotel_values(self):
        rows=facts({'transport_preferences':{'preferred':['walking','transit'],'forbidden':['taxi']},
                    'hotel_preferences':{'brand_mode':'required','preferred_brands':['如家']}})
        text=' '.join(r['value'] for r in rows)
        self.assertIn('步行、公共交通',text)
        self.assertIn('必须满足',text)
        self.assertNotIn('preferred',text)
        self.assertNotIn('forbidden',text)
