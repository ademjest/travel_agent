from copy import deepcopy
import unittest
from unittest.mock import Mock

import test_web_app as fixtures
from agents.trip_planner import TripPlanner
from core.tasks import TaskUpdate
from test_trips import PLACES,plan_value
from services.semantic_task_service import SemanticTaskService


class StateRoutingWebTests(unittest.TestCase):
    def setUp(self):
        self.web=fixtures.WebTests()
        self.web.setUp()
        self.addCleanup(self.web.doCleanups)
        w=self.web
        w.runtime.application.semantic_task_service=SemanticTaskService(w.store,Mock(),w.runtime.application.trip_service)
        event=w.event('创建行程','seed')
        spec={'destination':'武汉','start_date':'2030-10-01','day_count':3,'preferences':[],'must_visit':[]}
        trip={'trip_id':w.store.trips.new_id(event.event_key),'title':'测试行程','status':'active','spec':spec,
              'plan':TripPlanner.validate_plan(plan_value(),spec,PLACES),'sources':deepcopy(PLACES)}
        w.store.trips.commit(event,w.store.begin_event(event.event_key),TaskUpdate('itinerary','completed','创建',{}),'created',trip=trip)
        self.trip_id=trip['trip_id']

    def edit(self,key='edit',**extra):
        w=self.web
        return w.client.post(f'/api/conversations/{w.identity}/trips/{self.trip_id}/fields',headers=w.headers,
            json={'version':1,'field':'hotel_budget','value':'320','client_request_id':key,**extra})

    def test_queue_edit_context_confirm_and_duplicate(self):
        w=self.web
        first=self.edit().json()
        self.assertEqual(self.edit().json()['job_id'],first['job_id'])
        self.assertEqual(self.edit(value='350').status_code,409)
        self.assertTrue(w.run_job())
        context=w.repo.context(w.identity)
        self.assertEqual(context['trips'][0]['version'],1)
        self.assertTrue(context['trips'][0]['draft_facts'])
        preview=context['confirmations'][0]
        self.assertEqual(preview['diff']['base_version'],1)
        response=w.client.post(f'/api/conversations/{w.identity}/confirmations/{preview["id"]}',headers=w.headers,
            json={'version':preview['version'],'client_request_id':'confirm'})
        self.assertEqual(response.status_code,202)
        self.assertTrue(w.run_job())
        self.assertEqual(w.repo.context(w.identity)['trips'][0]['version'],2)
        self.assertEqual(self.edit().json()['job_id'],first['job_id'])

    def test_stale_draft_cannot_replace_latest_and_invalid_input_rejected(self):
        w=self.web
        self.assertEqual(self.edit(value='350-300').status_code,422)
        self.edit();w.run_job()
        self.edit('stale',value='350');w.run_job()
        self.assertIn('草稿已变化',str(w.messages()))
        draft=w.repo.context(w.identity)['trips'][0]['draft_facts']
        self.assertEqual(next(r['value'] for r in draft if r['field']=='budgets.accommodation'),'320 元/间/晚')

    def test_shadow_edit_finishes_with_explanation_without_mutation_or_retry(self):
        w=self.web
        w.runtime.application.semantic_task_service.mode='shadow'
        job=self.edit('shadow').json()['job_id']
        self.assertTrue(w.run_job())
        self.assertIn('观察模式',str(w.messages()))
        self.assertEqual(w.repo.job(job)['status'],'completed')
        context=w.repo.context(w.identity)
        self.assertEqual(context['confirmations'],[])
        self.assertEqual(context['trips'][0]['version'],1)
