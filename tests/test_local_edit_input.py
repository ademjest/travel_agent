from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from core.context_contract import complete_json
from core.model_budget import ModelBudgetExceeded
from tools.trip.constraints import apply_city_windows
from tools.trip.planner import TripPlanner
from test_draft_actions import PriceWorkflowTests
from test_travel_agent import FakeClient
from test_trips import model_response, plan_value, PLACES


class LocalEditTests(PriceWorkflowTests):
    # Reuse setup/interaction helpers, without rerunning the inherited test matrix.
    def test_reaffirmed_departure_and_corrected_meal_stay_local_after_clarification(self):
        self.create();self.dining();self.budget()
        task=self.web.store.trips.current_drafts(self.web.event(''))[0]
        slots=deepcopy(task.slots);candidate=slots['candidate_trip']
        window={'departure':{'day_index':3,'time_text':'下午','source':'第三天下午离开'}}
        apply_city_windows(candidate['spec'],window,'第三天下午离开')
        candidate['spec']['intercity_windows']=self.tool.planner.transport_windows(candidate['spec'],{})
        candidate['plan']['days'][-1]['activities']=candidate['plan']['days'][-1]['activities'][:1]
        candidate['plan']=self.tool.planner.update_recommendations(candidate['spec'],candidate['plan'],candidate['sources'],{'dining'})
        with self.web.store._connect() as db:
            db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        saved=self.save(self.context()['confirmations'][0]);before=deepcopy(saved)
        first='把第三天晚餐改成人均120元以内，其他部分不变，先生成预览。'
        reply=self.patch({'budget':{'scope':'food','value':'120元以内','unit_text':'人均','source':'第三天晚餐改成人均120元以内',
            'scopes':[{'day_index':3,'meal':'晚餐'}]}},first,'invalid-meal')
        self.assertIn('不在当前',reply)
        searches=self.amap.search_places.call_count;models=len(self.models.completions.requests)
        text='第三天还是下午离开，不安排晚餐。请只把第二天晚餐改成人均120元以内，先生成预览。'
        changes={'city_windows':{'value':{'departure':{'day_index':3,'time_text':'下午','source':'第三天还是下午离开'}},
                    'source':'第三天还是下午离开'},
                 'budget':{'scope':'food','value':'120元以内','unit_text':'人均','source':'第二天晚餐改成人均120元以内',
                    'scopes':[{'day_index':2,'meal':'晚餐'}]}}
        reply=self.patch(changes,text,'corrected-meal')
        self.assertIn('预览',reply)
        after=self.candidate()
        self.assertEqual(self.amap.search_places.call_count,searches)
        self.assertEqual(len(self.models.completions.requests),models)
        self.assertEqual(after['plan']['days'],before['plan']['days'])
        self.assertEqual(after['plan']['hotel_ids'],before['plan']['hotel_ids'])
        self.assertEqual(after['spec']['budgets']['food']['value'],before['spec']['budgets']['food']['value'])
        self.assertEqual(after['spec']['budgets']['food']['meals']['2:晚餐']['value'],'120元以内')
        self.assertNotIn('3:晚餐',after['spec']['budgets']['food'].get('meals',{}))
        for old,new in zip(before['plan']['dining_options'],after['plan']['dining_options']):
            if (old['day_index'],old['meal'])!=(2,'晚餐'):self.assertEqual(old,new)
        self.assertFalse(any(m['day_index']==3 and m['meal']=='晚餐' for m in after['plan']['dining_options']))
        self.assertEqual(self.web.store.trips.get(self.web.event(''),saved['trip_id'])['version'],saved['version'])
        with self.web.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM travel_tasks WHERE task_type='semantic' AND status='collecting'").fetchone()[0],0)

    def test_changed_city_window_is_not_treated_as_reaffirmation(self):
        self.create();base=self.candidate()
        apply_city_windows(base['spec'],{'departure':{'day_index':3,'time_text':'下午','source':'第三天下午离开'}},'第三天下午离开')
        self.tool._generate=Mock(return_value=(base['sources'],base['plan']))
        self.tool._edit_candidate(self.web.event('第三天晚上离开'),base,
            {'city_windows':{'value':{'departure':{'day_index':3,'time_text':'晚上','source':'第三天晚上离开'}},'source':'第三天晚上离开'}},
            '第三天晚上离开')
        self.tool._generate.assert_called_once()

    def test_projection_limit_is_visible_in_workflow_diagnostics(self):
        self.create()
        def oversized(*args,**kwargs):
            return complete_json({'requirements':'必须保留的约束'*100},20,purpose='trip_plan')
        self.tool._generate=Mock(side_effect=oversized)
        text='第三天晚上离开'
        reply=self.patch({'city_windows':{'value':{'departure':{'day_index':3,'time_text':'晚上','source':text}},
            'source':text}},text,'oversized')
        self.assertIn('系统组装',reply);self.assertNotIn('请分段',reply)
        with self.web.store._connect() as db:
            rows=[json.loads(row[0]) for row in db.execute("SELECT details_json FROM execution_diagnostics WHERE stage='planning'")]
        error=next(row for row in rows if row.get('error_code')=='input_projection_limit')
        self.assertGreater(error['validation']['actual'],error['validation']['max'])
        self.assertIn('requirements',error['input_parts'])


class PlanningInputTests(unittest.TestCase):
    def test_structural_request_omits_recommendation_duplicates_but_preserves_rules(self):
        places=deepcopy(PLACES)
        places.update({f'r{i}':{'id':f'r{i}','name':'餐馆'+str(i),'type':'餐饮服务','address':'地址'*150} for i in range(140)})
        previous=plan_value();previous['dining_options']=[{'details':'历史推荐'*15000}]
        spec={'destination':'武汉','day_count':3,'start_date':'2030-10-01','must_visit':['湖北省博物馆'],
              'source_context':'来源原文'*20000,
              'planning_evidence':{'h':[{'topic':'opening_hours','value_raw':'周一闭馆','status':'provider_answer_reference',
                  'scope':{'visit_date':'2030-10-01'},'evidence':[{'quote':'重复证据'*15000}]}]}}
        client=FakeClient([model_response(plan_value())]);planner=TripPlanner(client,'fixture',Mock())
        result=planner.plan(spec,places,previous,'调整整体游览顺序')
        payload=json.loads(client.completions.requests[0]['messages'][1]['content'])
        self.assertLess(len(client.completions.requests[0]['messages'][1]['content']),45000)
        self.assertNotIn('dining_options',payload['previous_plan'])
        self.assertFalse(any(p['id'].startswith('r') for p in payload['places']))
        rule=payload['requirements']['planning_evidence']['h'][0]
        self.assertEqual(rule['value_raw'],'周一闭馆');self.assertEqual(rule['status'],'provider_answer_reference')
        self.assertEqual(rule['scope']['visit_date'],'2030-10-01')
        self.assertIn('evidence',spec['planning_evidence']['h'][0])
        self.assertEqual(len(result['days']),3)

    def test_multiple_day_mentions_do_not_select_the_first_day_as_the_edit_target(self):
        client=FakeClient([model_response(plan_value())]);planner=TripPlanner(client,'fixture',Mock())
        planner._plan_day=Mock(side_effect=AssertionError('Must not pick the first mentioned day'))
        planner.plan({'destination':'武汉','day_count':3},PLACES,plan_value(),'第三天保持原样，调整第二天的安排')
        planner._plan_day.assert_not_called()

    def test_input_limit_reports_system_projection_size_without_requesting_shorter_user_text(self):
        with self.assertRaises(ModelBudgetExceeded) as caught:
            complete_json({'requirements':'约束'*20},20,purpose='trip_plan')
        self.assertGreater(caught.exception.details['actual'],caught.exception.details['max'])
        self.assertIn('requirements',caught.exception.input_parts)
        self.assertNotIn('请分段',str(caught.exception))

    def test_corrected_meal_replaces_pending_target_across_budget_container_forms(self):
        from tools.trip.constraints import merge_constraint_changes
        old={'budget':{'scope':'food','value':'120','scopes':[{'day_index':3,'meal':'晚餐'}]}}
        new={'budgets':[{'scope':'food','value':'120','scopes':[{'day_index':2,'meal':'晚餐'}]}]}
        merged=merge_constraint_changes(old,new,correcting_meal_window=True)
        self.assertNotIn('budget',merged)
        self.assertEqual(merged['budgets'],new['budgets'])

    def test_new_free_text_requirement_is_not_discarded_as_metadata(self):
        from tools.trip.constraints import effective_changes
        change={'requirements':{'value':'不去室外','source':'不去室外'}}
        self.assertEqual(effective_changes({'preferences':['轻松一点']},change,'不去室外'),change)


# Only the added cases use PriceWorkflowTests as a fixture mixin.
def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(PlanningInputTests))
    for name in LocalEditTests.__dict__:
        if name.startswith('test_'):suite.addTest(LocalEditTests(name))
    return suite
