"""Real Web queue and confirmation transaction, with deterministic public provider fixtures."""
from copy import deepcopy
from datetime import datetime,timezone
import json
import unittest
from urllib.parse import quote
from unittest.mock import Mock

from agents.travel_agent import TravelAgent
from core.intent_ir import parse_intent_ir
from core.settings import Settings
from infrastructure.wendao_client import WendaoClient,ENDPOINT
from tools.hotel_prices.tool import HotelQuotes
from test_draft_actions import WorkflowHarness
import test_hotel_quotes as fixtures
from test_travel_agent import FakeClient,completion,assistant_message,tool_call
from test_trips import model_response


class AccommodationWebTests(WorkflowHarness,unittest.TestCase):
    def test_query_replace_preview_exact_save_and_two_originals(self):
        self.create()
        task=self.web.store.trips.current_drafts(self.web.event(''))[0]
        slots=deepcopy(task.slots);base=slots['candidate_trip']
        stay={**fixtures.STAY,'check_in':'2030-10-01','check_out':'2030-10-03'}
        original={**fixtures.TARGET['h'],'cityname':'武汉'}
        base['sources']['h']=original;base['spec'].update(hotel_requested=True,hotel_stay=stay,
            budgets={'accommodation':{'value':'300-400','unit_text':'人民币每间每晚'}})
        base['plan'].update(hotel_ids=['h'],hotel_recommendations=[])
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots,ensure_ascii=False),task.task_id))
        raw,offer=fixtures.fixture()
        def convert(value):
            for old,new in (('测试城','武汉'),(original['name'],'替代酒店'),('测试路8号','测试路9号'),
                            ('2030-01-07','2030-10-01'),('2030-01-09','2030-10-03'),('2030-01-08','2030-10-02'),
                            ('2030年1月7日','2030年10月1日'),('2030年1月9日','2030年10月3日'),('1月7日','10月1日'),('1月8日','10月2日')):
                value=value.replace(old,new)
            return value
        raw=convert(raw);offer=json.loads(convert(json.dumps(offer,ensure_ascii=False)));offer['target_id']='replacement'
        target={**original,'id':'replacement','name':'替代酒店','address':'测试路9号'}
        transport=Mock(side_effect=[('原门店目标日期无房价资料'.encode(),ENDPOINT,'text/markdown'),(raw.encode(),ENDPOINT,'text/markdown')])
        extractor=FakeClient([model_response({'offers':[]}),model_response({'offers':[offer]})])
        amap=Mock();amap.search_places.return_value=[original,target]
        hotel=HotelQuotes(self.web.store,WendaoClient('synthetic',True,transport=transport),extractor,'test',amap=amap)
        self.tool.hotel_quotes=hotel;self.tool.clock=lambda:datetime.now(timezone.utc)
        self.tool._reminder_changes=Mock(return_value=([],[],[],{},None))
        self.web.runtime.travel_service.hotel_prices=hotel;self.web.runtime.tool_router.trip_tool=self.tool
        calls=[('query_hotel_prices',{}),('query_hotel_prices',{'discovery':{'city':'武汉','area':'行程附近'}}),
               ('preview_accommodation_candidates',{'candidate_ids':['replacement']})]
        model=FakeClient([completion(assistant_message(tool_calls=[tool_call(str(i),name,json.dumps(args,ensure_ascii=False))])) for i,(name,args) in enumerate(calls)])
        self.tool.travel_agent=TravelAgent(Settings('','',frozenset(),'map','key','https://invalid','test'),self.web.runtime.tool_router.execute_result,client=model)
        text='请查询这些酒店候选房价，如果不满足预算则重新选择酒店'
        self.compiler.try_compile.return_value=parse_intent_ir({'schema_version':1,'action':'update','operations':[
            {'domain':'trip','operation':'prepare_accommodation','request_text':text,'allow_preview':True,'replace_only_if_budget_mismatch':True}],
            'source_spans':[text],'requires_confirmation':True},text)
        self.assertEqual(self.web.send(text,key='hotel-goal').status_code,202);self.assertTrue(self.web.run_job())
        reply=self.web.messages()[-1]
        self.assertIn('核对后可保存',reply['content'])
        self.assertEqual(len(reply['hotel_actions']),2)
        for action in reply['hotel_actions']:
            endpoint=f'/api/conversations/{self.web.identity}/hotel-quotes/{quote(action["source_event_id"],safe="")}/original'
            page=self.web.client.get(endpoint)
            self.assertEqual(page.status_code,200);self.assertTrue(page.json()['text'])
        self.assertEqual(transport.call_count,2);self.assertEqual(len(model.completions.requests),3)
        new=self.context()['confirmations'][0];before=self.candidate()
        self.assertEqual(before['plan']['hotel_ids'],['replacement'])
        for key in ('days','intercity','restaurant_ids','dining_options'):
            self.assertEqual(before['plan'].get(key),base['plan'].get(key))
        saved=self.save(new,'save-accommodation')
        self.assertEqual(saved['spec'],before['spec']);self.assertEqual(saved['plan'],before['plan'])
        self.assertEqual(transport.call_count,2);self.assertEqual(len(model.completions.requests),3)
        self.assertEqual(len(extractor.completions.requests),2)
