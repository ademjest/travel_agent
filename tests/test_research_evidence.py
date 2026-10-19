import json
import unittest

from core.research_evidence import validate_research_items
from services.research_service import ResearchService
from test_travel_agent import FakeClient, assistant_message, completion


class ResearchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.text = '门票票价80元人民币。无需预约。每日09:00开放，17:00闭馆。'
        self.source = {'id': 1, 'status': 'read', 'text': self.text, 'url': 'https://example.test/policy',
                       'title': '测试资料', 'published_at': '', 'retrieved_at': '2030-09-09'}

    def item(self, text, quote=None):
        value = {'text': text, 'sources': [1]}
        if quote is not None:
            value['evidence'] = [{'source_id': 1, 'quote': quote}]
        return value

    def test_general_suggestion_keeps_legacy_compatibility(self):
        value = self.item('建议慢慢游览，开放时间待核实。')
        self.assertEqual(validate_research_items([value], [self.source]), [value])

    def test_concrete_claim_requires_actual_excerpt(self):
        for value in (self.item('票价80元。'), self.item('票价80元。', '票价是80元'),
                      self.item('票价100元。', self.text)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_research_items([value], [self.source])
        self.assertEqual(len(validate_research_items([self.item('票价80元人民币。', '门票票价80元人民币。')], [self.source])), 1)

    def test_unknown_free_admission_policy_is_not_a_free_admission_assertion(self):
        quote='门票票价80元人民币。'
        validate_research_items([self.item('票价80元人民币；资料未说明优惠或免票政策。',quote)],[self.source])
        for text in ('票价80元人民币；儿童免票，资料未说明其他优惠政策。',
                     '票价80元人民币；资料未说明优惠或免票政策，但儿童免费。'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                validate_research_items([self.item(text,quote)],[self.source])

    def test_opening_and_closing_times_cannot_be_swapped(self):
        validate_research_items([self.item('09:00开放，17:00闭馆。', self.text)], [self.source])
        validate_research_items([self.item('9:00开放，17:00闭馆。', self.text)], [self.source])
        with self.assertRaises(ValueError):
            validate_research_items([self.item('17:00开放，09:00闭馆。', self.text)], [self.source])

    def test_reservation_negation_and_no_advance_rule_are_distinct(self):
        source = {**self.source, 'text': '需要预约，但无需提前预约。'}
        with self.assertRaises(ValueError):
            validate_research_items([self.item('无需预约。', source['text'])], [source])
        validate_research_items([self.item('需要预约，无需提前预约。', source['text'])], [source])

    def test_snippet_cannot_confirm_price_or_reservation(self):
        with self.assertRaises(ValueError):
            validate_research_items([self.item('无需预约。', '无需预约。')], [{**self.source, 'status': 'snippet'}])

    def test_quote_cannot_reference_unread_tail_or_wrong_source(self):
        source = {**self.source, 'text': '甲' * 4500 + self.text}
        with self.assertRaises(ValueError):
            validate_research_items([self.item('票价80元。', self.text)], [source])
        item = self.item('票价80元。', self.text)
        item['evidence'][0]['source_id'] = 2
        with self.assertRaises(ValueError):
            validate_research_items([item], [self.source, {**self.source, 'id': 2}])

    def test_quote_cannot_cut_negation_or_condition(self):
        for body, quote, claim in [('不需要预约。', '需要预约', '需要预约。'),
                ('如果天气允许，09:00开放。', '09:00开放', '09:00开放。')]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_research_items([self.item(claim, quote)], [{**self.source, 'text': body}])
        source = {**self.source, 'text': '如果天气允许，09:00开放。'}
        with self.assertRaises(ValueError):
            validate_research_items([self.item('09:00开放。', source['text'])], [source])
        validate_research_items([self.item(source['text'], source['text'])], [source])

    def test_audience_or_weekday_price_scope_cannot_be_dropped(self):
        for quote in ('儿童票价80元。', '周六票价80元。'):
            source = {**self.source, 'text': quote}
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                validate_research_items([self.item('票价80元。', quote)], [source])
            validate_research_items([self.item(quote, quote)], [source])

    def test_currency_symbols_and_chinese_hours_cannot_bypass_evidence(self):
        for text in ('门票¥80。', '免费入场。', '上午九点开放。'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_research_items([self.item(text)], [self.source])
        source = {**self.source, 'text': '上午九点开放。'}
        with self.assertRaises(ValueError):
            validate_research_items([self.item('上午八点开放。', source['text'])], [source])
        source = {**self.source, 'text': '儿童免票。'}
        with self.assertRaises(ValueError):
            validate_research_items([self.item('免票。', source['text'])], [source])

    def test_amount_and_units_cannot_be_borrowed_from_other_object(self):
        source = {**self.source, 'text': '住宿每间每晚360元，餐饮每人每天80元。'}
        with self.assertRaises(ValueError):
            validate_research_items([self.item('住宿每人每天360元。', source['text'])], [source])

    def test_research_cannot_claim_it_performed_booking(self):
        with self.assertRaises(ValueError):
            validate_research_items([self.item('已经为你预约成功。', self.text)], [self.source])

    def test_unproven_policy_paraphrase_is_replaced_by_visible_original(self):
        source = {**self.source, 'text': '周一闭馆，其他日期请查看公告。'}
        items = validate_research_items([self.item('每天开放。', source['text'])], [source])
        self.assertEqual(items[0]['text'], '来源原文：' + source['text'])
        self.assertNotIn('每天开放', json.dumps(items, ensure_ascii=False))

    def test_failed_claim_is_not_displayed_or_forwarded_to_planning(self):
        service = ResearchService(None, None, FakeClient([completion(assistant_message(content=json.dumps(
            {'items': [self.item('门票100元。', self.text)]}, ensure_ascii=False)))]), 'offline')
        accepted = []
        report = service._summarize('测试资料', [self.source], evidence_sink=accepted)
        self.assertNotIn('门票100元', report)
        self.assertIn('未通过', report)
        self.assertEqual(accepted, [])

    def test_planning_gets_complete_claim_and_evidence_with_visible_omission(self):
        source = {**self.source, 'text': self.text * 7}
        items = [self.item('建议：票价80元。' + '背景' * 280, source['text']) for _ in range(8)]
        context = ResearchService._planning_source({'report': '不可截断伪造的任意正文',
                                                   'report_items': items, 'sources': [source]})
        payload = json.loads(context.split('\n', 1)[1])
        self.assertGreater(payload['omitted_items'], 0)
        self.assertLessEqual(len(context), 6000)
        self.assertTrue(payload['items'])
        self.assertIn('evidence', payload['items'][0])
        self.assertEqual(payload['sources'][0]['url'], self.source['url'])
