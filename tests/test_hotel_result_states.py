"""No-price declarations require source evidence; failures never manufacture unavailability."""
from copy import deepcopy
from pathlib import Path
import json
import unittest
from tools.hotel_prices.quotes import validate_quote_facts,project_quote
from tools.hotel_prices.quotes import evaluate
from tools.hotel_prices.tool import quote_text
from test_hotel_quotes import TARGET,STAY,fixture


class HotelResultStateTests(unittest.TestCase):
    def test_real_display_rate_is_visible_without_becoming_a_dated_room_quote(self):
        directory=Path(__file__).parent/'fixtures'
        data=json.loads((directory/'wendao_display_rate_s4_2026-11-08.json').read_text(encoding='utf-8'))
        raw=(directory/'wendao_display_rate_s4_2026-11-08.md').read_text(encoding='utf-8');before=deepcopy(data['extracted_facts'])
        result=validate_quote_facts(data['extracted_facts'],raw,data['targets'],data['stay'],data['city'])
        self.assertFalse(result['offers']);self.assertEqual(len(result['reference_clues']),1,result['rejected'])
        clue=result['reference_clues'][0]
        self.assertEqual(clue['reference_amounts'],['347']);self.assertEqual(clue['nightly_prices'],[])
        self.assertEqual(clue['applicability_status'],'not_verified')
        snapshot={**data,**result,'raw_text':raw}
        self.assertEqual(evaluate({'destination':data['city'],'hotel_stay':data['stay']},snapshot),[])
        text=quote_text({'destination':data['city'],'hotel_stay':data['stay']},snapshot)
        self.assertIn('347 CNY',text);self.assertIn('来源展示金额',text);self.assertIn('计价口径待核对',text)
        self.assertEqual(data['extracted_facts'],before)
        invalid=deepcopy(before);invalid['offers'][0]['nightly_prices'][0]['amount']='25'
        rejected=validate_quote_facts(invalid,raw,data['targets'],data['stay'],data['city'])
        self.assertFalse(rejected['offers']);self.assertFalse(rejected['reference_clues'])
        changed=deepcopy(before);offer=changed['offers'][0]
        old='约 ¥347 RMB（携程展示最低价）';new='未知；2026-11-10早餐最低价25 RMB'
        breakfast_raw=raw.replace(old,new)
        offer.update(price_row=offer['price_row'].replace(old,new),terms_text=offer['terms_text'].replace(old,new),reported_total=None)
        offer['nightly_prices'][0]['amount']='25'
        rejected=validate_quote_facts(changed,breakfast_raw,data['targets'],data['stay'],data['city'])
        self.assertFalse(rejected['offers']);self.assertFalse(rejected['reference_clues'])
    def test_real_s4_target_date_absence_survives_rejected_generic_start_rates(self):
        directory=Path(__file__).parent/'fixtures'
        data=json.loads((directory/'wendao_no_price_s4_2026-11-08.json').read_text(encoding='utf-8'))
        raw=(directory/'wendao_no_price_s4_2026-11-08.md').read_text(encoding='utf-8')
        result=validate_quote_facts(data['extracted_facts'],raw,data['targets'],data['stay'],data['city'])
        self.assertEqual(len(result['unavailable_prices']),3);self.assertEqual(len(result['rejected']),3)
        self.assertFalse(result['offers']);self.assertFalse(result['reference_clues'])
        projected=project_quote({**data,**result,'raw_text':raw})
        self.assertEqual(projected['result_status'],'source_price_unknown')
        text=quote_text({'destination':data['city'],'hotel_stay':data['stay']},projected)
        self.assertIn('部分房型',text);self.assertIn('展示起价不等于',text)
        for target in data['targets'].values():self.assertIn(target['name'],text)

    def test_source_absence_and_a_different_priced_product_can_coexist(self):
        raw,offer=fixture();raw+='\n### 另一房型\n- 每晚价格：未知\n'
        facts={'offers':[offer],'price_absence':[{'target_id':'h','evidence_text':raw}]}
        result=validate_quote_facts(facts,raw,TARGET,STAY,'测试城')
        self.assertEqual(len(result['offers']),1);self.assertEqual(len(result['unavailable_prices']),1)
        projected=project_quote({**result,'raw_text':raw})
        self.assertEqual(projected['result_status'],'offers_available');self.assertIn('部分房型',projected['unavailable_note'])
    def sample(self):
        raw='### 云泽酒店(湖畔店)\n测试城测试路8号\n2030-01-07入住，2030-01-09退房。\n- **每晚价格**：未知（工具未返回该日期实时房价）\n- 早餐：25元/人\n'
        return raw,{'offers':[],'price_absence':[{'target_id':'h','evidence_text':raw}]}

    def test_explicit_source_absence_is_distinct_from_a_validation_failure(self):
        raw,facts=self.sample();result=validate_quote_facts(facts,raw,TARGET,STAY,'测试城')
        snapshot={'raw_text':raw,'targets':TARGET,'stay':STAY,'city':'测试城','extracted_facts':facts,**result}
        self.assertEqual(len(result['unavailable_prices']),1);self.assertFalse(result['reference_clues'])
        self.assertEqual(project_quote(snapshot)['result_status'],'source_price_unknown')
        text=quote_text({'hotel_stay':STAY},snapshot)
        self.assertIn('房价标注为未知',text);self.assertIn('早餐费用不作为住宿房价',text)
        self.assertNotIn('25 CNY',text);self.assertNotIn('已售罄',text)

    def test_breakfast_unknown_alone_does_not_prove_missing_room_prices(self):
        raw,facts=self.sample();raw=raw.replace('- **每晚价格**：未知（工具未返回该日期实时房价）','- 早餐：未知')
        facts['price_absence'][0]['evidence_text']=raw
        result=validate_quote_facts(facts,raw,TARGET,STAY,'测试城')
        self.assertFalse(result['unavailable_prices']);self.assertTrue(result['rejected'])

    def test_absence_cannot_borrow_another_hotel_or_dates_or_invent_a_quote(self):
        raw,facts=self.sample()
        for old,new in [('云泽酒店(湖畔店)','另一酒店'),('2030-01-07','2030-01-06'),('未知（工具未返回该日期实时房价）','399元')]:
            value=deepcopy(facts);value['price_absence'][0]['evidence_text']=raw.replace(old,new)
            result=validate_quote_facts(value,value['price_absence'][0]['evidence_text'],TARGET,STAY,'测试城')
            self.assertFalse(result['unavailable_prices']);self.assertFalse(result['reference_clues'])

    def test_extraction_failure_with_source_money_does_not_claim_provider_lacks_prices(self):
        raw,_=fixture();snapshot={'raw_text':raw,'offers':[],'extraction_status':'failed'}
        projected=project_quote(snapshot)
        self.assertEqual(projected['result_status'],'extraction_failed');self.assertIn('不能据此断言',projected['result_note'])
        self.assertEqual(snapshot,{'raw_text':raw,'offers':[],'extraction_status':'failed'})

    def test_not_submitted_unknown_submission_and_budget_block_are_separate(self):
        for snapshot,status in [({},'not_submitted'),({'request_id':'x','submission_status':'submission_unknown'},'submission_unknown'),
            ({'raw_text':'已保存资料','budget_block':{'code':'run_call_limit'}},'budget_blocked')]:
            self.assertEqual(project_quote(snapshot)['result_status'],status)

    def test_empty_extraction_alone_does_not_prove_provider_unavailability(self):
        snapshot={'raw_text':'某房型房费399元','offers':[],'extracted_facts':{'offers':[]}}
        self.assertEqual(project_quote(snapshot)['result_status'],'no_structured_price')

    def test_unpriced_product_placeholder_is_not_an_available_quote(self):
        raw,offer=fixture();offer.update(nightly_prices=[],reported_total=None)
        result=validate_quote_facts({'offers':[offer]},raw,TARGET,STAY,'测试城')
        self.assertFalse(result['offers']);self.assertFalse(result['reference_clues'])
        self.assertEqual(project_quote({**result,'raw_text':raw})['result_status'],'no_structured_price')


if __name__=='__main__':unittest.main()
