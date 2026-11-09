"""A faithful source amount is visible without becoming a verified POI/budget offer."""
from copy import deepcopy
from pathlib import Path
import json
import unittest
from unittest.mock import Mock

from tools.hotel_prices.quotes import validate_quote_facts,validate_quotes,evaluate,same_address
from tools.hotel_prices.tool import quote_text,reference_text,HotelQuotes
from core.chat_transport import OutgoingMessage
from infrastructure.wendao_client import WendaoClient,ENDPOINT
from test_travel_agent import FakeClient
from test_trips import model_response
import test_hotel_quotes as fixtures

DIR=Path(__file__).parent/'fixtures'
DATA=json.loads((DIR/'wendao_hotel_identity_2026-11-07.json').read_text(encoding='utf-8'))
RAW=(DIR/'wendao_hotel_identity_2026-11-07.md').read_text(encoding='utf-8')

def legacy_snapshot():
    return {**{k:deepcopy(DATA[k]) for k in ('targets','stay','retrieved_at','rejected')},'city':'武汉','offers':[],'raw_text':RAW,
        'requested_hotels':{pid:p['name'] for pid,p in DATA['targets'].items()},'source_event_id':'public-saved-source'}


class ReferenceEvidenceTests(unittest.TestCase):
    def validate(self,value=None,raw=RAW,targets=None):
        return validate_quote_facts(deepcopy(value or DATA['constructed_extraction']),raw,targets or DATA['targets'],DATA['stay'],'武汉')

    def test_saved_public_price_is_a_clue_not_a_bound_offer(self):
        result=self.validate()
        self.assertEqual(result['offers'],[]);self.assertEqual(len(result['reference_clues']),1)
        clue=result['reference_clues'][0]
        self.assertEqual(clue['nightly_prices'],[{'date':'2026-11-09','amount':'210','unit':'CNY/room/night'}])
        self.assertEqual(clue['identity_status'],'unverified');self.assertNotIn('amap_poi_id',clue)
        self.assertFalse(same_address(clue['address'],next(iter(DATA['targets'].values())),'武汉'))
        snapshot={**legacy_snapshot(),**result}
        spec={'destination':'武汉','hotel_stay':DATA['stay'],'budgets':{'accommodation':{'value':'200-230','unit_text':'人民币每间每晚'}}}
        self.assertEqual(evaluate(spec,snapshot),[])
        text=quote_text(spec,snapshot)
        self.assertIn('210',text);self.assertIn('不用于预算通过',text);self.assertIn('税费及完整应付金额待确认',text)

    def test_legacy_api_stays_strict_and_source_spans_locate_all_clue_facts(self):
        offers,rejected=validate_quotes(DATA['constructed_extraction'],RAW,DATA['targets'],DATA['stay'],'武汉')
        self.assertEqual(offers,[]);self.assertEqual(rejected[0]['code'],'address_unverified')
        clue=self.validate()['reference_clues'][0]
        for span in clue['evidence_spans']:self.assertEqual(RAW[span['start']:span['end']],span['text'])

    def test_unknown_fees_with_independently_matching_address_is_not_budget_pass(self):
        targets=deepcopy(DATA['targets']);pid=next(iter(targets))
        targets[pid]['address']=DATA['constructed_extraction']['offers'][0]['address']
        result=self.validate(targets=targets)
        self.assertEqual(len(result['offers']),1);self.assertEqual(result['reference_clues'],[])
        spec={'destination':'武汉','hotel_stay':DATA['stay'],'budgets':{'accommodation':{'value':'200-230','unit_text':'人民币每间每晚'}}}
        evaluated=evaluate(spec,{**legacy_snapshot(),**result})
        self.assertFalse(any(o['status']=='reference_match' for o in evaluated))

    def test_invented_amount_or_wrong_currency_has_no_clue(self):
        for mutate in (lambda o:o['nightly_prices'][0].update(amount='211'),lambda o:o.update(currency='USD')):
            value=deepcopy(DATA['constructed_extraction']);mutate(value['offers'][0])
            result=self.validate(value);self.assertEqual(result['reference_clues'],[]);self.assertEqual(result['offers'],[])

    def test_other_branch_or_city_cannot_be_presented_as_this_hotel(self):
        for fields in ({'hotel_name':'另一家酒店'},{'city':'另一城市'}):
            value=deepcopy(DATA['constructed_extraction']);value['offers'][0].update(fields)
            result=self.validate(value);self.assertEqual(result['reference_clues'],[])

    def test_breakfast_money_cannot_become_a_dated_room_price(self):
        value=deepcopy(DATA['constructed_extraction']);offer=value['offers'][0]
        raw=RAW.replace('**210 RMB**（2026-11-09 晚）','未知；早餐费用40 RMB/人')
        row=offer['price_row'].replace('**210 RMB**（2026-11-09 晚）','未知；早餐费用40 RMB/人')
        offer.update(price_row=row,terms_text=row,nightly_prices=[{'date':'2026-11-09','amount':'40'}])
        result=self.validate(value,raw);self.assertEqual(result['offers'],[]);self.assertEqual(result['reference_clues'],[])

    def test_legacy_snapshot_shows_unchanged_source_without_fabricated_structured_price(self):
        snapshot=legacy_snapshot();before=deepcopy(snapshot)
        text=quote_text({'destination':'武汉','hotel_stay':DATA['stay']},snapshot)
        self.assertNotIn('210 RMB',text);self.assertIn('查看原文',text)
        self.assertNotIn('本批没有可验证的结构化报价；',text)
        self.assertEqual(snapshot,before);self.assertNotIn('reference_clues',snapshot)

    def test_unparsed_source_does_not_mix_another_hotel_into_summary(self):
        snapshot=legacy_snapshot();snapshot['raw_text']+='\n### 另一家酒店(东门店)\n每晚99999元\n'
        text=reference_text(snapshot)
        self.assertIn('查看原文',text);self.assertNotIn('99999',text);self.assertNotIn('210 RMB',text)

    def test_model_style_floor_detail_is_not_removed_to_make_identity_pass(self):
        target=next(iter(DATA['targets'].values()))
        self.assertFalse(same_address(DATA['constructed_extraction']['offers'][0]['address'],target,'武汉'))
        self.assertEqual(self.validate()['offers'],[])


class ReferenceProjectionStoreTests(unittest.TestCase):
    setUp=fixtures.QuoteLookupTests.setUp

    def test_future_extraction_saves_one_fact_output_and_projects_one_layer(self):
        self.transport.return_value=(RAW.encode(),ENDPOINT,'text/markdown')
        self.service.client=FakeClient([model_response(DATA['constructed_extraction'])])
        event=self.event
        snapshot=self.service.lookup(event,{'city':'武汉','stay':DATA['stay']},targets=DATA['targets'],policy={'requested':True},refresh=True)
        self.assertEqual(snapshot['offers'],[]);self.assertEqual(len(snapshot['reference_clues']),1)
        self.assertEqual(snapshot['extracted_facts'],DATA['constructed_extraction'])
        stored=self.store.research.hotel_quote_for_event(event,event.event_key)
        self.assertEqual(len(stored['reference_clues']),1);self.assertEqual(stored['extracted_facts'],DATA['constructed_extraction'])
        self.assertEqual(self.transport.call_count,1);self.assertEqual(len(self.service.client.completions.requests),1)

    def test_legacy_message_gets_a_local_reference_preview_without_requery(self):
        self.transport.return_value=(RAW.encode(),ENDPOINT,'text/markdown')
        self.wendao.query(self.event,self.store.research,'合成已有来源',purpose='hotel_quotes',fingerprint='saved')
        snapshot=legacy_snapshot();snapshot['source_event_id']=self.event.event_key
        self.store.research.hotel_extraction(self.event,result={'status':'completed','snapshot':snapshot})
        self.service.enabled=False;self.service.client=None
        before=self.transport.call_count
        actions=self.service.actions(self.event,None,self.event.event_key)
        self.assertIn('查看原文',actions[0]['reference_text']);self.assertNotIn('210 RMB',actions[0]['reference_text']);self.assertEqual(self.transport.call_count,before)
        stored=self.store.research.hotel_quote_for_event(self.event,self.event.event_key)
        self.assertEqual(stored['offers'],[]);self.assertNotIn('reference_clues',stored)

    def test_history_hotel_filter_also_filters_reference_clues(self):
        snapshot={**legacy_snapshot(),**validate_quote_facts(DATA['constructed_extraction'],RAW,DATA['targets'],DATA['stay'],'武汉')}
        pid=next(iter(DATA['targets']))
        snapshot['requested_hotels']['other']='另一家酒店(东门店)'
        snapshot['reference_clues'].append({**snapshot['reference_clues'][0],'requested_target_ref':'other','hotel_name':'另一家酒店(东门店)'})
        self.store.research.hotel_quote_records=Mock(return_value=[{'extraction':{'snapshot':snapshot}}])
        saved=self.service.history(self.event,{'targets':[{'name':DATA['targets'][pid]['name']}]})[0]
        self.assertEqual([c['requested_target_ref'] for c in saved['reference_clues']],[pid])
        self.assertEqual(len(snapshot['reference_clues']),2)


def seed_saved_message(web,snapshot,key='saved-reference'):
    """Public fixed receipt in a temporary database; no provider/model/network call."""
    event=web.event('读取公开酒店参考资料',key)
    web.store.delivery.begin_event(event.event_key)
    vendor=Mock(return_value=(RAW.encode(),ENDPOINT,'text/markdown'))
    WendaoClient('test',True,transport=vendor).query(event,web.store.research,'公开固定回执',purpose='hotel_quotes',fingerprint=key)
    snapshot=deepcopy(snapshot);snapshot['source_event_id']=event.event_key
    web.store.research.hotel_extraction(event,result={'status':'completed','snapshot':snapshot})
    text=quote_text({'destination':'武汉','hotel_stay':DATA['stay']},snapshot) if snapshot.get('evidence_version') else '旧回执：门店城市、完整名称或地址不一致，尚未唯一绑定。'
    native=web.repo.deliver(OutgoingMessage('group',web.identity,event.event_id,{'kind':'reply','text':text},key))
    with web.store._connect() as db:
        db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',('web',event.storage_scope_id,native,event.event_key,event.sender_id))
    return event,text,snapshot


class ReferenceWebTests(unittest.TestCase):
    def setUp(self):
        from test_web_app import WebTests
        self.web=WebTests();self.web.setUp();self.addCleanup(self.web.doCleanups)

    def test_old_message_get_shows_saved_source_without_rewriting_it(self):
        event,text,snapshot=seed_saved_message(self.web,legacy_snapshot())
        message=self.web.messages()[-1]
        self.assertEqual(message['content'],text)
        action=message['hotel_actions'][0]
        self.assertIn('查看原文',action['reference_text']);self.assertNotIn('210 RMB',action['reference_text'])
        self.assertEqual(self.web.store.research.hotel_quote_for_event(event,event.event_key),snapshot)
        url=f"/api/conversations/{self.web.identity}/hotel-quotes/{action['source_event_id']}/original?revision={action['revision']}"
        self.assertIn('210 RMB',self.web.client.get(url).json()['text'])
        other=self.web.client.post('/api/conversations',headers=self.web.headers).json()['id']
        self.assertEqual(self.web.client.get(url.replace(self.web.identity,other)).status_code,404)

    def test_new_message_has_one_reference_layer_and_no_extra_legacy_preview(self):
        from tools.hotel_prices.quotes import EVIDENCE_VERSION
        snapshot={**legacy_snapshot(),**validate_quote_facts(DATA['constructed_extraction'],RAW,DATA['targets'],DATA['stay'],'武汉'),
            'evidence_version':EVIDENCE_VERSION}
        event,text,_=seed_saved_message(self.web,snapshot,'new-reference')
        message=self.web.messages()[-1]
        self.assertIn('210 CNY',text);self.assertIn('不用于预算通过',text)
        self.assertNotIn('reference_text',message['hotel_actions'][0])
        self.assertEqual(self.web.store.research.hotel_quote_for_event(event,event.event_key)['offers'],[])


if __name__=='__main__':unittest.main()
