"""Saved model outputs and bounded format variants; no supplier/model calls."""
from copy import deepcopy
from pathlib import Path
import json
import unittest
from unittest.mock import Mock

from tools.hotel_prices.quotes import validate_quote_facts
from tools.hotel_prices.tool import quote_text
from tools.hotel_prices.quotes import project_quote

DIR=Path(__file__).parent/'fixtures'
DATA=json.loads((DIR/'wendao_hotel_single_night_2026-11-07.json').read_text(encoding='utf-8'))
RAW=(DIR/'wendao_hotel_single_night_2026-11-07.md').read_text(encoding='utf-8')


def variant(replacements,*,nights=1):
    raw=RAW;facts=deepcopy(DATA['extracted_facts']);stay=deepcopy(DATA['stay'])
    for old,new in replacements:
        raw=raw.replace(old,new)
        for offer in facts['offers']:
            for key,value in offer.items():
                if isinstance(value,str):offer[key]=value.replace(old,new)
    if nights==2:
        stay['check_out']='2026-11-11'
        for offer in facts['offers']:
            offer['nightly_prices']=[{'date':'2026-11-09','amount':'210'},{'date':'2026-11-10','amount':'210'}]
    return raw,facts,stay


class HotelResponseFormatTests(unittest.TestCase):
    def check(self,raw=RAW,facts=None,stay=None):
        return validate_quote_facts(facts or DATA['extracted_facts'],raw,DATA['targets'],stay or DATA['stay'],DATA['city'])

    def test_actual_single_night_separate_date_and_price_lines(self):
        result=self.check()
        self.assertEqual(result['offers'],[])
        self.assertEqual(len(result['reference_clues']),2,result['rejected'])
        self.assertTrue(all(r['amount_qualifier']=='approximate' for r in result['reference_clues']))
        snapshot={**DATA,**result,'raw_text':RAW,'requested_hotels':{k:v['name'] for k,v in DATA['targets'].items()}}
        text=quote_text({'destination':'武汉','hotel_stay':DATA['stay']},snapshot)
        self.assertIn('| 房型 |',text);self.assertIn('210 CNY',text)
        self.assertNotIn('客房总数',text);self.assertNotIn('> ',text)

    def test_markdown_and_field_punctuation_variants(self):
        raw,facts,stay=variant([('**每晚日期价格**：约210元人民币/晚','**每晚价格及币种**: 约 **210** 元人民币/晚')])
        clues=self.check(raw,facts,stay)['reference_clues']
        self.assertEqual(len(clues),2)
        self.assertTrue(all(c['amount_qualifier']=='approximate' for c in clues))

    def test_bound_products_still_display_the_approximate_qualifier(self):
        targets=deepcopy(DATA['targets'])
        targets[next(iter(targets))]['address']=DATA['extracted_facts']['offers'][0]['address']
        result=validate_quote_facts(DATA['extracted_facts'],RAW,targets,DATA['stay'],DATA['city'])
        self.assertEqual(len(result['offers']),2)
        text=quote_text({'destination':'武汉','hotel_stay':DATA['stay']},{**DATA,**result,'targets':targets,'raw_text':RAW})
        self.assertGreaterEqual(text.count('约 210 CNY'),2)

    def test_explicit_uniform_multi_night_remains_supported(self):
        raw,facts,stay=variant([('2026-11-10','2026-11-11'),('共1晚','共2晚'),
            ('约210元人民币/晚','各晚均为约210元人民币/晚')],nights=2)
        self.assertEqual(len(self.check(raw,facts,stay)['reference_clues']),2)

    def test_multiple_nights_without_uniform_or_dated_prices_are_not_invented(self):
        raw,facts,stay=variant([('2026-11-10','2026-11-11'),('共1晚','共2晚')],nights=2)
        self.assertEqual(self.check(raw,facts,stay)['reference_clues'],[])

    def test_markdown_cleanup_does_not_join_separate_numbers(self):
        raw,facts,stay=variant([('约210元人民币/晚','约2*10元人民币/晚')])
        self.assertEqual(self.check(raw,facts,stay)['reference_clues'],[])

    def test_breakfast_amount_is_not_a_single_night_room_rate(self):
        raw,facts,stay=variant([('**每晚日期价格**：约210元人民币/晚','**每晚日期价格**：未知\n- **早餐费用**：210元')])
        self.assertEqual(self.check(raw,facts,stay)['reference_clues'],[])

    def test_conflicting_product_dates_do_not_inherit_global_dates(self):
        raw,facts,stay=variant([('**入住/退房日期**：2026-11-09入住，2026-11-10退房','**入住/退房日期**：2026-11-08入住，2026-11-09退房')])
        self.assertEqual(self.check(raw,facts,stay)['reference_clues'],[])

    def test_other_room_amount_cannot_fill_missing_price(self):
        facts=deepcopy(DATA['extracted_facts']);first=facts['offers'][0]
        old=first['price_row'];new=old.replace('约210元人民币/晚','未知')
        raw=RAW.replace(old,new);first.update(price_row=new,terms_text=new)
        result=self.check(raw,facts)
        self.assertEqual([r['room_name'] for r in result['reference_clues']],['双床房（无早餐）'])

    def test_no_structured_facts_yields_short_status_not_raw_dump(self):
        snapshot={**DATA,'raw_text':RAW,'offers':[],'reference_clues':[],
            'requested_hotels':{k:v['name'] for k,v in DATA['targets'].items()}}
        text=quote_text({'destination':'武汉','hotel_stay':DATA['stay']},snapshot)
        self.assertIn('查看原文',text);self.assertNotIn('客房总数',text)
        self.assertNotIn('约210',text)

    def test_saved_real_extraction_is_rechecked_without_mutating_history(self):
        snapshot={**deepcopy(DATA),'raw_text':RAW,'offers':[],'reference_clues':[],
            'evidence_version':'hotel-reference-evidence-v1'}
        before=deepcopy(snapshot);projected=project_quote(snapshot)
        self.assertEqual(snapshot,before)
        self.assertEqual(len(projected['reference_clues']),2)
        self.assertEqual(projected['offers'],[])


class SavedHotelPresentationTests(unittest.TestCase):
    def setUp(self):
        from test_web_app import WebTests
        self.web=WebTests();self.web.setUp();self.addCleanup(self.web.doCleanups)

    def test_current_api_shows_compact_recheck_and_preserves_message_and_source(self):
        from infrastructure.wendao_client import WendaoClient,ENDPOINT
        from core.chat_transport import OutgoingMessage
        from core.tool_result import ToolResult
        event=self.web.event('确认酒店房价','saved-real-facts')
        store=self.web.store;store.delivery.begin_event(event.event_key)
        transport=Mock(return_value=(RAW.encode(),ENDPOINT,'text/markdown'))
        WendaoClient('fixture',True,transport=transport).query(event,store.research,'固定公开回执',purpose='hotel_quotes',fingerprint='fixture')
        snapshot={**deepcopy(DATA),'raw_text':RAW,'offers':[],'reference_clues':[],
            'requested_hotels':{k:v['name'] for k,v in DATA['targets'].items()},
            'evidence_version':'hotel-reference-evidence-v1','source_event_id':event.event_key}
        store.research.hotel_extraction(event,result={'status':'completed','snapshot':snapshot})
        text='问道参考报价 · 查询时间\n'+RAW
        receipt=ToolResult('completed','query_hotel_prices',data=text,payload={'snapshot':snapshot})
        store.delivery.save_event_tool_result('saved-receipt',event.event_key,'query_hotel_prices',{},receipt.to_json())
        native=self.web.repo.deliver(OutgoingMessage('group',self.web.identity,event.event_id,{'kind':'reply','text':text},'saved-receipt'))
        with store._connect() as db:
            db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',('web',event.storage_scope_id,native,event.event_key,event.sender_id))
        before=deepcopy(store.research.hotel_quote_for_event(event,event.event_key))
        message=self.web.messages()[-1]
        self.assertEqual(message['content'],text)
        self.assertIn('| 房型 |',message['hotel_presentation']['text'])
        self.assertIn('210 CNY',message['hotel_presentation']['text'])
        self.assertNotIn('客房总数',message['hotel_presentation']['text'])
        self.assertEqual(store.research.hotel_quote_for_event(event,event.event_key),before)
        service=self.web.runtime.travel_service.hotel_prices
        self.assertIsNone(service.presentation(event,event.event_key,text+'\n额外天气结果'))
        action=message['hotel_actions'][0]
        original=self.web.client.get(f'/api/conversations/{self.web.identity}/hotel-quotes/{event.event_key}/original?revision={action["revision"]}')
        self.assertEqual(original.status_code,200);self.assertEqual(original.json()['text'],RAW)
        self.assertEqual(transport.call_count,1)
