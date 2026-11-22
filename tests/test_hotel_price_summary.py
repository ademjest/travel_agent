"""Readable provider prices do not require, or become, exact nightly offers."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tools.hotel_prices.quotes import project_quote,evaluate
from tools.hotel_prices.tool import quote_text
from test_hotel_quotes import TARGET

SAVED=json.loads((Path(__file__).parent/'fixtures/wendao_price_summary_2026-11-12.json').read_text(encoding='utf-8'))['snapshots']


class HotelPriceSummaryTests(unittest.TestCase):
    def test_actual_three_reference_prices_are_visible_without_invented_nights(self):
        original=deepcopy(SAVED[1]);snapshot=project_quote(original)
        text=quote_text({'destination':'南京','hotel_stay':snapshot['stay']},snapshot)
        for value in ('280','222','514','携程问道回答','仅供参考'):
            self.assertIn(value,text)
        self.assertEqual(snapshot['offers'],[])
        self.assertFalse(evaluate({'hotel_stay':snapshot['stay']},snapshot))
        self.assertEqual(original,SAVED[1])
        for summary in snapshot['price_summaries']:
            for span in summary['evidence']:
                self.assertEqual(snapshot['raw_text'][span['start']:span['end']],span['text'])

    def test_actual_from_and_bed_prices_keep_their_scope(self):
        snapshot=project_quote(SAVED[0]);text=quote_text({'hotel_stay':snapshot['stay']},snapshot)
        self.assertIn('49元',text);self.assertIn('床位',text);self.assertIn('仅限1人',text)
        self.assertIn('196元',text);self.assertIn('起步价',text)
        self.assertIn('未查到价格',text)
        self.assertNotIn('98元',text)
        self.assertEqual(snapshot['offers'],[])

    def test_other_hotel_and_ancillary_costs_are_not_room_prices(self):
        raw='## 云泽酒店(湖畔店)\n- **房价**：未知，早餐25元，停车30元。\n- 电话：025-12345678\n## 其他酒店\n- 每晚价格：999元\n'
        snapshot=project_quote({'raw_text':raw,'targets':TARGET,'requested_hotels':{'h':TARGET['h']['name']},'offers':[]})
        self.assertEqual(snapshot['price_summaries'][0]['status'],'not_found')
        text=quote_text({},snapshot)
        self.assertIn('未查到价格',text);self.assertNotIn('999',text);self.assertNotIn('25元',text)

    def test_price_not_attributable_is_not_called_absent(self):
        raw='## 云泽酒店(湖畔店)\n每晚价格：399元\n## 云泽酒店(湖畔店)\n每晚价格：499元\n'
        snapshot=project_quote({'raw_text':raw,'targets':TARGET,'offers':[]})
        self.assertEqual(snapshot['price_summaries'][0]['status'],'unresolved')
        self.assertNotIn('未查到价格',quote_text({},snapshot))

    def test_fee_amount_order_and_included_breakfast(self):
        for value,expected in [('未知，25元早餐，30元停车费','not_found'),('280元含早餐','reference'),('含早餐约280元/晚','reference')]:
            with self.subTest(value=value):
                result=project_quote({'raw_text':'## 云泽酒店(湖畔店)\n房价：'+value,'targets':TARGET})
                self.assertEqual(result['price_summaries'][0]['status'],expected)

    def test_table_preserves_dates_room_type_and_unknown_terms(self):
        raw='## 云泽酒店(湖畔店)\n| 房型 | 11月17日 | 11月18日 | 税费 |\n| --- | --- | --- | --- |\n| 双床房 | 280元起 | 390元 | 未知 |'
        result=project_quote({'raw_text':raw,'targets':TARGET})
        text=quote_text({},result)
        self.assertIn('11月17日：280元起',text);self.assertIn('11月18日：390元',text)
        self.assertIn('双床房',text);self.assertIn('税费：未知',text)
        self.assertFalse(result.get('offers'))

    def test_unrecognized_price_is_pending_and_wrong_city_is_not_borrowed(self):
        for raw in ['## 云泽酒店(湖畔店)\n双床房280元/晚', '## 云泽酒店(湖畔店)\n城市：其他市\n房价：280元']:
            with self.subTest(raw=raw):
                result=project_quote({'raw_text':raw,'targets':TARGET})
                self.assertEqual(result['price_summaries'][0]['status'],'unresolved')
                self.assertNotIn('未查到价格',quote_text({},result))

    def test_number_without_unit_does_not_invent_currency(self):
        result=project_quote({'raw_text':'## 云泽酒店(湖畔店)\n房价：280','targets':TARGET})
        text=quote_text({},result)
        self.assertIn('280',text);self.assertIn('原文未明确币种或单位',text)
        self.assertNotIn('280元',text)

    def test_currency_prefix_and_free_extras_are_not_confused(self):
        for value,expected in [('CNY 280/晚','reference'),('免费早餐，房费待确认','not_found'),('免费升级房型，房价未知','not_found')]:
            with self.subTest(value=value):
                result=project_quote({'raw_text':'## 云泽酒店(湖畔店)\n房价：'+value,'targets':TARGET})
                self.assertEqual(result['price_summaries'][0]['status'],expected)


if __name__=='__main__':unittest.main()
