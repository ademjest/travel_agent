"""Automatic quote lookup belongs to lodging edits, never to passive reads."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import unittest
from unittest.mock import Mock

import test_hotel_quotes as fixtures
from test_hotel_quotes import trip
from tools.trip.tool import TripTool
from tools.hotel_prices.tool import quote_text


class AutomaticHotelQuoteTests(unittest.TestCase):
    def setUp(self):
        fixtures.QuoteLookupTests.setUp(self)
        self.planner=Mock()
        self.planner.reconcile_candidate.side_effect=lambda candidate,previous:candidate
        self.tool=TripTool(self.store,self.planner,hotel_quotes=self.service)
        self.tool._reminder_changes=Mock(return_value=([],[],[],{},None))
        self.tool._question=Mock(return_value='preview')
        self.tool.format_trip=Mock(return_value='preview')

    def preview(self,candidate,*,current=None,requested=False,event=None):
        self.tool._preview_candidate(event or self.event,None,candidate,current=current,quote_requested=requested)
        return self.tool._question.call_args.args[3]['candidate_trip']

    def test_new_hotel_plan_without_budget_queries_and_remains_unconfirmed(self):
        t=trip();t['spec'].pop('budgets')
        result=self.preview(t)
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(len(result['plan']['hotel_quotes']['offers']),1)
        self.assertNotIn('budgets',result['spec'])
        self.assertFalse(self.store.trips.list_for_owner(self.event))

    def test_repeated_hotel_request_on_legacy_plan_queries_even_without_field_diff(self):
        current=trip();candidate=deepcopy(current)
        result=self.preview(candidate,current=current,requested=True)
        self.assertEqual(self.transport.call_count,1)
        self.assertTrue(result['plan']['hotel_quotes']['raw_text'])
        self.assertIn('hotel_quotes',result['answer_changes'],'A quote-only update must appear in the concise answer')
        self.assertNotIn('hotel_quotes',current['plan'])
        self.preview(deepcopy(result),current=result,requested=True,event=replace(self.event,event_id='again'))
        self.assertEqual(self.transport.call_count,1,'Unchanged conditions must reuse a fresh quote')

    def test_unrelated_preview_never_queries_missing_or_stale_prices(self):
        current=trip()
        self.preview(deepcopy(current),current=current)
        self.transport.assert_not_called()
        quoted=fixtures.lookup_hotel_quotes(self.service,self.event,trip())
        quoted['retrieved_at']=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
        current['plan']['hotel_quotes']=quoted
        self.preview(deepcopy(current),current=current,event=replace(self.event,event_id='unrelated'))
        self.assertEqual(self.transport.call_count,1)

    def test_missing_stay_asks_once_and_does_not_claim_a_supplier_result(self):
        current=trip();current['spec'].pop('hotel_stay')
        result=self.preview(deepcopy(current),current=current,requested=True)
        self.transport.assert_not_called()
        text=quote_text(result['spec'],result['plan']['hotel_quotes'])
        self.assertIn('尚未查询',text)
        self.assertEqual(text.count('请一并补充'),1)
        self.assertIn('房间数',text)
        self.assertNotIn('查询时间',text)
        self.assertNotIn('本批未找到',text)

    def test_disabled_and_source_restricted_automatic_lookup_make_no_call(self):
        current=trip();self.service.enabled=False
        self.preview(deepcopy(current),current=current,requested=True)
        self.transport.assert_not_called()
        self.service.enabled=True
        event=replace(self.event,content='只用高德更换酒店，不使用问道')
        self.preview(deepcopy(current),current=current,requested=True,event=event)
        self.transport.assert_not_called()

    def test_failed_request_is_distinct_from_never_submitted(self):
        text=quote_text(trip()['spec'],{'request_id':'failed','offers':[],
            'note':'问道未完成：result_unknown；不自动重试。'})
        self.assertIn('未取得可用结果',text)
        self.assertNotIn('尚未查询',text)
        self.assertNotIn('查询时间 未知',text)
