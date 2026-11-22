"""Reference prices appear in the first Web draft without becoming budget-qualified rates."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from core.intent_ir import IntentIR,IntentOperation
from infrastructure.wendao_client import WendaoClient,ENDPOINT
from tools.hotel_prices.tool import HotelQuotes
from test_draft_actions import WorkflowHarness
from test_hotel_quotes import TARGET
from test_trips import SPEC,PLACES,plan_value,model_response
from test_travel_agent import FakeClient


class HotelPriceSummaryWebTests(WorkflowHarness,unittest.TestCase):
    def test_initial_budget_draft_shows_reference_and_save_does_not_query(self):
        text='帮我规划2030年10月1日开始的武汉三天行程，带老人，必去湖北省博物馆，不查询跨城交通，酒店每间每晚300-400元，先给草案'
        source='酒店每间每晚300-400元'
        spec={**SPEC,'start_date_text':'2030年10月1日','preferences':['带老人',source],
            'intercity':{'scope':{'value':'none','source':'不查询跨城交通'}},
            'budgets':[{'scope':'accommodation','value':'300-400','unit_text':'人民币每间每晚','source':source}]}
        self.models.completions.responses=[model_response(spec),model_response(plan_value())]
        target={**TARGET['h'],'id':'lodging','cityname':'武汉'}
        self.amap.search_places.return_value=[*PLACES.values(),target]
        raw='## 云泽酒店(湖畔店)\n每晚价格：约350元起，税费未知，指定日期房型待核对'
        transport=Mock(return_value=(raw.encode(),ENDPOINT,'text/markdown'))
        extractor=FakeClient([])
        hotel=HotelQuotes(self.web.store,WendaoClient('synthetic',True,transport=transport),extractor,'test')
        self.tool.hotel_quotes=hotel;self.tool.travel_agent=Mock()
        self.web.runtime.travel_service.hotel_prices=hotel
        self.tool._reminder_changes=Mock(return_value=([],[],[],{},None))
        self.compiler.try_compile.return_value=IntentIR(1,'create',(IntentOperation('trip','create',{'request_text':text}),),source_spans=(text,))
        self.assertEqual(self.web.send(text,key='first-price-draft').status_code,202)
        self.assertTrue(self.web.run_job())
        reply=self.web.messages()[-1]['content']
        self.assertIn('约350元起',reply);self.assertIn('仅供参考',reply)
        self.assertEqual(transport.call_count,1);self.assertEqual(extractor.completions.requests,[])
        self.tool.travel_agent.run.assert_not_called()
        candidate=self.candidate()
        self.assertEqual(candidate['plan']['hotel_ids'],[])
        self.assertTrue(candidate['plan']['hotel_investigation'])
        self.assertFalse(candidate['plan']['hotel_quotes']['offers'])
        self.assertEqual(candidate['plan']['hotel_quotes']['coverage']['returned'],1)
        self.assertFalse(candidate['spec'].get('hotel_stay'))
        self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))
        before=deepcopy(candidate)
        saved=self.save(self.context()['confirmations'][0],'save-reference')
        self.assertEqual(saved['plan'],before['plan'])
        self.assertEqual(saved['spec'],before['spec'])
        self.assertEqual(transport.call_count,1);self.assertEqual(extractor.completions.requests,[])


if __name__=='__main__':unittest.main()
