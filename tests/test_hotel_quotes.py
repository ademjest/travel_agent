from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from concurrent.futures import ThreadPoolExecutor

from core.chat_transport import ChatEvent
from core.intent_ir import trip_read_check,IntentIR,IntentOperation
from infrastructure.memory_store import MemoryStore
from infrastructure.wendao_client import WendaoClient,ENDPOINT
from tools.hotel_prices.quotes import validate_quotes, evaluate, amount, safe_url
from tools.hotel_prices.tool import HotelQuotes, quote_text
from tools.trip.constraints import stay_change, missing_stay
from tools.trip.planner import assess_hotels,TripPlanner
from tools.trip.constraints import apply_constraints
from tools.trip.tool import TripTool, lookup_hotel_quotes
from test_travel_agent import FakeClient
from test_trips import model_response
from test_draft_actions import WorkflowHarness

NOW=datetime(2030,1,1,8,tzinfo=timezone.utc)
STAY={'check_in':'2030-01-07','check_out':'2030-01-09','rooms':1,'adults':2,'children_ages':[]}
TARGET={'h':{'id':'h','name':'云泽酒店(湖畔店)','address':'测试路8号','cityname':'测试城','type':'住宿服务;酒店','rating':'4.8'}}
HEADER='| 房型 | 1月7日 | 1月8日 | 总价 | 条款 |'
def fixture(a=399,b=399,terms='含全部税费，无早餐，18点前可取消',total=None):
    total=total if total is not None else a+b
    row=f'| 标准双床房 | {a}元 | {b}元 | {total}元 | {terms} |'
    stay='2030年1月7日入住，2030年1月9日退房，1间房，2名成人，无儿童。'
    text='### 云泽酒店(湖畔店)\n地址：测试城测试路8号\n'+stay+'\n'+HEADER+'\n|\n- |\n'+row+'\nhttps://hotels.ctrip.com/hotels/1234.html'
    offer={'target_id':'h','hotel_name':TARGET['h']['name'],'address':'测试路8号','city':'测试城',
        'hotel_text':text,'stay_text':stay,**STAY,'room_name':'标准双床房','price_basis':'per_room_night','currency':'CNY',
        'nightly_prices':[{'date':'2030-01-07','amount':str(a)},{'date':'2030-01-08','amount':str(b)}],
        'reported_total':str(total),'price_header':HEADER,'price_row':row,'terms_text':row,
        'taxes_and_fees_status':'included' if '含税' in terms or '含全部税费' in terms else 'unknown','breakfast':'无早餐' if '无早餐' in terms else None,
        'cancellation':'18点前可取消' if '18点前可取消' in terms else None,'eligibility':None,
        'source_url':'https://hotels.ctrip.com/hotels/1234.html','provider_hotel_id':'1234'}
    return text,offer
def spec(budget='350-450'):
    return {'destination':'测试城','day_count':1,'hotel_requested':True,'hotel_stay':deepcopy(STAY),
        'budgets':{'accommodation':{'value':budget,'unit_text':'人民币每间每晚'}}}
def snapshot(a=399,b=399,terms='含全部税费，无早餐，18点前可取消',total=None):
    raw,item=fixture(a,b,terms,total);offers,rejected=validate_quotes({'offers':[item]},raw,TARGET,STAY,'测试城')
    assert not rejected,rejected
    return {'offers':offers,'stay':deepcopy(STAY),'retrieved_at':NOW.isoformat(),'raw_text':raw}
def trip():
    return {'trip_id':'TR-000000000001','version':1,'status':'active','title':'测试行程','spec':spec(),
            'plan':{'days':[{'day_index':1,'date':'2030-01-07','activities':[]}],'hotel_ids':['h']},'sources':deepcopy(TARGET)}


class QuoteRulesTests(unittest.TestCase):
    def test_three_way_quote_facts_are_atomic_and_newer_source_wins(self):
        from tools.trip.constraints import rebase_candidate
        base=trip();base['plan']['hotel_quotes']=snapshot();base['plan']['hotel_quotes']['request_id']='old'
        local=deepcopy(base);local['spec']['budgets']['accommodation']['value']='450以内'
        current=deepcopy(base);current['plan']['hotel_quotes']=snapshot(499,499)
        current['plan']['hotel_quotes'].update(request_id='new',retrieved_at=(NOW+timedelta(minutes=1)).isoformat())
        result,conflicts=rebase_candidate(base,local,current)
        self.assertFalse(conflicts);self.assertEqual(result['plan']['hotel_quotes'],current['plan']['hotel_quotes'])
        self.assertEqual(result['spec']['budgets']['accommodation']['value'],'450以内')

    def test_broken_table_real_sample_rejects_provider_budget_conclusion(self):
        raw=(Path(__file__).parent/'fixtures/wendao_hotels_public_2026-11-05.md').read_text(encoding='utf-8')
        data=json.loads((Path(__file__).parent/'fixtures/wendao_hotels_public_2026-11-05.json').read_text(encoding='utf-8'))
        offers,rejected=validate_quotes({'offers':data['offers']},raw,data['targets'],data['stay'],'南京')
        self.assertEqual(rejected,[]);self.assertEqual(len(offers),6)
        s=spec();s['hotel_stay']=data['stay']
        result=evaluate(s,{'offers':offers,'stay':data['stay'],'retrieved_at':'2026-11-05T00:00:00+00:00'},datetime(2026,11,5,0,5,tzinfo=timezone.utc))
        self.assertEqual({o['numeric_comparison'] for o in result if o['amap_poi_id']=='hanting'},{'below'})
        self.assertFalse(any(o['status']=='reference_match' for o in result))
        self.assertTrue(all(o['taxes_and_fees_status']=='unknown' for o in result))

    def test_strict_bounds_and_each_night_not_average(self):
        for budget,price,expected in [('350-450',346,'below'),('450以内',450,'within'),('低于450',450,'over'),
                ('不少于350',350,'within'),('高于350',350,'below'),('400左右',400,'not_comparable')]:
            with self.subTest(budget=budget):self.assertEqual(evaluate(spec(budget),snapshot(price,price),NOW)[0]['numeric_comparison'],expected)
        result=evaluate(spec('500以内'),snapshot(399,599),NOW)[0]
        self.assertEqual(result['numeric_comparison'],'over')
        self.assertEqual(result['status'],'excluded')

    def test_unknown_taxes_missing_dates_and_conflicting_total_are_not_matches(self):
        self.assertEqual(evaluate(spec(),snapshot(),NOW)[0]['status'],'reference_match')
        self.assertEqual(evaluate(spec(),snapshot(terms='退改未知'),NOW)[0]['status'],'pending')
        self.assertEqual(evaluate(spec(),snapshot(terms='不含税'),NOW)[0]['status'],'pending')
        self.assertEqual(evaluate(spec(),snapshot(total=900),NOW)[0]['applicability_status'],'total_conflict')
        raw,item=fixture();item['nightly_prices'].pop()
        offers,rejected=validate_quotes({'offers':[item]},raw,TARGET,STAY,'测试城')
        self.assertEqual(rejected,[]);self.assertEqual(offers[0]['applicability_status'],'incomplete_conditions')
        self.assertEqual(evaluate(spec('300以内'),{'offers':offers,'stay':STAY,'retrieved_at':NOW.isoformat()},NOW)[0]['status'],'pending')

    def test_dates_cells_terms_and_branches_cannot_be_borrowed(self):
        raw,item=fixture(399,599)
        for mutation in (lambda i:i.update(address='另一分店'),lambda i:i.update(city='异城'),
                lambda i:i['nightly_prices'][0].update(amount='599'),lambda i:i['nightly_prices'].append(i['nightly_prices'][0]),
                lambda i:i.update(hotel_name='云泽酒店'),lambda i:i.update(terms_text='无依据的可退')):
            bad=deepcopy(item);mutation(bad)
            offers,rejected=validate_quotes({'offers':[bad]},raw,TARGET,STAY,'测试城')
            self.assertFalse(offers);self.assertTrue(rejected)
        raw,item=fixture();target=deepcopy(TARGET);target['h'].update(cityname='测试城',adname='湖区')
        item['address']='测试城湖区测试路8号'
        raw=raw.replace('地址：测试城测试路8号','地址：测试城湖区测试路8号');item['hotel_text']=raw
        offers,rejected=validate_quotes({'offers':[item]},raw,target,STAY,'测试城')
        self.assertFalse(rejected);self.assertEqual(len(offers),1)
        item['address']='异城湖区测试路8号';item['hotel_text']=raw.replace('测试城湖区测试路8号',item['address'])
        offers,rejected=validate_quotes({'offers':[item]},item['hotel_text'],target,STAY,'测试城')
        self.assertFalse(offers)
        # Prose has the same date/amount pairing and product boundaries as table cells.
        raw,item=fixture(399,599)
        product='### 标准双床房\n1月7日 399 RMB/晚；1月8日 599 RMB/晚\n含税，总价998元'
        identity='### 云泽酒店(湖畔店)\n地址：测试城测试路8号'
        raw=identity+'\n'+item['stay_text']+'\n'+product+'\n'+item['source_url']
        item.update(hotel_text=identity,price_header='',price_row=product,terms_text=product,
                    breakfast=None,cancellation=None)
        offers,rejected=validate_quotes({'offers':[item]},raw,TARGET,STAY,'测试城')
        self.assertFalse(rejected);self.assertEqual(len(offers),1)
        swapped=deepcopy(item);swapped['nightly_prices'][0]['amount']='599'
        self.assertFalse(validate_quotes({'offers':[swapped]},raw,TARGET,STAY,'测试城')[0])
        second='\n### 豪华大床房\n1月7日 399 RMB/晚；1月8日 599 RMB/晚\n含税'
        mixed=deepcopy(item);mixed.update(price_row=product+second,terms_text=product+second)
        self.assertFalse(validate_quotes({'offers':[mixed]},raw.replace(product,product+second),TARGET,STAY,'测试城')[0])

    def test_bad_numbers_currency_and_links(self):
        for value in (None,True,-1,'NaN','Infinity','1.001'):
            with self.assertRaises(ValueError):amount(value)
        for url in ('http://hotels.ctrip.com/x','https://127.0.0.1/x','javascript:alert(1)','https://ctrip.com.evil.test','https://a@ctrip.com/x'):
            self.assertIsNone(safe_url(url))
        foreign=snapshot();foreign['offers'][0]['currency']='USD'
        self.assertEqual(evaluate(spec(),foreign,NOW)[0]['numeric_comparison'],'not_comparable')
        raw,item=fixture();raw+='\nhttps://hotels.ctrip.com/hotels/5678.html';item['hotel_text']=raw
        second=deepcopy(item);second.update(source_url='https://hotels.ctrip.com/hotels/5678.html',provider_hotel_id='5678')
        offers,rejected=validate_quotes({'offers':[item,second]},raw,TARGET,STAY,'测试城')
        self.assertFalse(rejected);self.assertTrue(all(o['identity_status']=='ambiguous' for o in offers))
        result=evaluate(spec(),{'offers':offers,'stay':STAY,'retrieved_at':NOW.isoformat()},NOW)
        self.assertTrue(all(o['status']=='pending' and o['numeric_comparison']=='not_comparable' for o in result))

    def test_expiry_and_changed_conditions_do_not_reuse_or_mutate_history(self):
        q=snapshot();before=deepcopy(q)
        self.assertEqual(evaluate(spec(),q,NOW+timedelta(minutes=31))[0]['status'],'historical')
        s=spec();s['hotel_stay']['adults']=3
        self.assertEqual(evaluate(s,q,NOW)[0]['status'],'different_stay')
        s=spec('400以内');self.assertEqual(evaluate(s,q,NOW)[0]['status'],'reference_match')
        from tools.trip.constraints import itinerary_calendar
        s=spec();s['hotel_stay']['itinerary_calendar']=itinerary_calendar(s)
        s['start_date']='2030-02-01'
        self.assertEqual(evaluate(s,q,NOW)[0]['status'],'different_stay')
        self.assertTrue(any('行程日期' in issue for issue in missing_stay(s,NOW)))
        self.assertEqual(q,before)

    def test_membership_and_hard_room_terms_stay_pending(self):
        q=snapshot(terms='含税，会员券后价')
        self.assertEqual(evaluate(spec(),q,NOW)[0]['status'],'pending')
        s=spec();s['hotel_stay']['room_requirement']='大床房'
        self.assertEqual(evaluate(s,snapshot(),NOW)[0]['status'],'different_stay')
        raw,item=fixture(terms='含全部税费，不含早，不可取消');item.update(breakfast='不含早',cancellation='不可取消')
        offers,rejected=validate_quotes({'offers':[item]},raw,TARGET,STAY,'测试城');self.assertFalse(rejected)
        for key,value in [('breakfast_requirement','含早'),('cancellation_requirement','可取消')]:
            current=spec();current['hotel_stay'][key]=value
            q={'offers':offers,'stay':deepcopy(current['hotel_stay']),'retrieved_at':NOW.isoformat()}
            self.assertEqual(evaluate(current,q,NOW)[0]['status'],'pending')

    def test_total_only_cannot_prove_per_night_budget(self):
        raw,item=fixture();item.update(price_basis='total',nightly_prices=[])
        offers,rejected=validate_quotes({'offers':[item]},raw,TARGET,STAY,'测试城')
        self.assertFalse(rejected)
        q={'offers':offers,'stay':deepcopy(STAY),'retrieved_at':NOW.isoformat()}
        self.assertEqual(evaluate(spec(),q,NOW)[0]['status'],'pending')
        self.assertIn('供应商全程总价',quote_text(spec(),q,NOW))
        s=spec('800以内');s['budgets']['accommodation']['unit_text']='人民币全程总价'
        self.assertEqual(evaluate(s,q,NOW)[0]['status'],'reference_match')

    def test_stay_sources_and_no_default_occupancy(self):
        self.assertEqual(len(missing_stay({})),5)
        text='2030年1月7日入住，2030年1月9日退房，1间房，2名成人，无儿童'
        change={'value':STAY,'source':text,'field_sources':{k:text for k in STAY}}
        result,_=apply_constraints(spec(),{'hotel_stay':change},text)
        self.assertEqual(result['hotel_stay']['rooms'],1)
        bad=deepcopy(change);bad['value']['adults']=1
        with self.assertRaises(ValueError):stay_change({},bad,text)
        s=spec();s['hotel_stay']['rooms']=2;self.assertTrue(missing_stay(s))
        s=spec();s['hotel_stay']['children_ages']=None;self.assertTrue(missing_stay(s))

    def test_read_contract_requires_explicit_refresh_and_budget_stays_saved(self):
        self.assertEqual(trip_read_check({'scope':'accommodation','source':'之前的报价'},'之前的报价').get('mode','saved'),'saved')
        self.assertEqual(trip_read_check({'scope':'accommodation','source':'刷新房价','mode':'refresh'},'刷新房价')['mode'],'refresh')
        for source in ('查看预算','不要刷新房价','只解释旧价格'):
            with self.assertRaises(ValueError):trip_read_check({'scope':'accommodation','source':source,'mode':'refresh'},source)


class QuoteLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=MemoryStore(Path(self.temp.name)/'test.db')
        self.event=ChatEvent('onebot','group','quote-event','scope','owner','刷新酒店入住日报价')
        self.raw,self.item=fixture()
        self.transport=Mock(return_value=(self.raw.encode(),ENDPOINT,'text/markdown'))
        self.wendao=WendaoClient('test-token',True,transport=self.transport)
        self.client=FakeClient([model_response({'offers':[self.item]})])
        self.service=HotelQuotes(self.store,self.wendao,self.client,'test')

    def test_different_purposes_replay_and_query_conflict(self):
        self.wendao.query(self.event,self.store.research,'景点研究')
        result=lookup_hotel_quotes(self.service,self.event,trip(),refresh=True)
        self.assertEqual(len(result['offers']),1)
        self.assertEqual(self.transport.call_count,2)
        replay=lookup_hotel_quotes(self.service,self.event,trip(),refresh=True)
        self.assertEqual(replay['offers'],result['offers']);self.assertEqual(self.transport.call_count,2)
        changed=trip();changed['spec']['hotel_stay']['adults']=3
        conflict=lookup_hotel_quotes(self.service,self.event,changed,refresh=True)
        self.assertIn('冲突',conflict['note']);self.assertEqual(self.transport.call_count,2)

    def test_public_request_boundary_and_view_isolation(self):
        t=trip();private='为私密旅客姓名挑选非标准房型，不对外披露'
        t['spec']['hotel_stay'].update(room_requirement=private,field_sources={'room_requirement':{'text':'不需要外发的原话'}})
        original=deepcopy(t);text='请查询房价，这次按1名成人试算，不修改行程'
        event=replace(self.event,content=text)
        tool=TripTool(self.store,TripPlanner(None,'test',None),hotel_quotes=self.service)
        checks=[{'scope':'accommodation','source':text,'mode':'refresh','hotel_stay':{
            'value':{'adults':1},'source':'1名成人','field_sources':{'adults':'1名成人'}}}]
        def view(ev,value,prefix=''):
            self.assertEqual(value,original);return prefix
        with patch.object(tool,'view',side_effect=view):tool._trip_read_reply(event,t,checks,True)
        self.assertEqual(t,original)
        query=self.transport.call_args.kwargs['payload']['query']
        model_input=self.client.completions.requests[0]['messages'][1]['content']
        self.assertNotIn(private,query);self.assertNotIn(private,model_input)
        self.assertNotIn('field_sources',model_input)
        self.assertIn('其他要求仅在本地保留',query)

    def test_cache_budget_change_new_event_no_supplier_or_model(self):
        first=lookup_hotel_quotes(self.service,self.event,trip())
        other=replace(self.event,event_id='next')
        t=trip();t['spec']['budgets']['accommodation']['value']='380以内'
        second=lookup_hotel_quotes(self.service,other,t)
        self.assertTrue(second['reused']);self.assertEqual(first['request_fingerprint'],second['request_fingerprint'])
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(evaluate(t['spec'],second,NOW)[0]['numeric_comparison'],'over')
        # A saved extraction failure is also a bounded negative cache, not permission to pay again.
        with self.store._connect() as db:
            row=db.execute('SELECT result_json FROM wendao_requests').fetchone()
            data=json.loads(row['result_json']);data['extraction']['snapshot']['offers']=[]
            db.execute('UPDATE wendao_requests SET result_json=?',(json.dumps(data),))
        failed=lookup_hotel_quotes(self.service,replace(self.event,event_id='negative-cache'),t)
        self.assertTrue(failed['reused']);self.assertEqual(self.transport.call_count,1)

    def test_disabled_missing_and_source_restrictions_never_submit(self):
        for text in ('只用高德查询酒店','不联网，只看已有资料','只用官网'):
            self.assertIn('限制',lookup_hotel_quotes(self.service,replace(self.event,content=text),trip(),refresh=True)['note'])
        t=trip();t['spec'].pop('hotel_stay')
        self.assertIn('一并补充',lookup_hotel_quotes(self.service,self.event,t)['note'])
        self.transport.assert_not_called()

    def test_failed_extraction_and_unknown_submission_never_retry(self):
        self.client=FakeClient([model_response({'offers':[{'bad':True}]})])
        self.service.client=self.client
        first=lookup_hotel_quotes(self.service,self.event,trip(),refresh=True)
        self.assertFalse(first['offers'])
        lookup_hotel_quotes(self.service,self.event,trip(),refresh=True)
        self.assertEqual(self.transport.call_count,1)
        other=replace(self.event,event_id='unknown')
        key={'status':'submitted','query_hash':'hash','fingerprint':'fp'}
        self.store.research.wendao_call(other,reserve=key,purpose='hotel_quotes',query_hash='hash',fingerprint='fp')
        row=self.store.research.wendao_call(other,purpose='hotel_quotes',query_hash='hash',fingerprint='fp')[0]
        self.assertEqual(row['status'],'submitted')

    def test_concurrent_purpose_reservations_and_owner_isolation(self):
        def reserve(purpose):return self.store.research.wendao_call(self.event,reserve={'status':'submitted','query_hash':purpose},purpose=purpose,query_hash=purpose)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,['research','hotel_quotes']))
        self.assertEqual(sum(reserved for _,reserved in results),2)
        with self.assertRaises(ValueError):reserve('other')
        self.assertFalse(self.store.research.hotel_quote_records(replace(self.event,sender_id='other')))
        self.assertEqual(len(self.store.research.hotel_quote_records(self.event)),1)

    def test_readonly_refresh_does_not_change_trip_and_saved_read_has_zero_calls(self):
        t=trip();before=deepcopy(t)
        tool=TripTool(self.store,TripPlanner(None,'test',None),hotel_quotes=self.service)
        reply=tool._trip_read_reply(self.event,t,[{'scope':'accommodation','source':'刷新房价','mode':'refresh'}],False)
        self.assertIn('399',reply);self.assertEqual(t,before)
        saved=deepcopy(t);saved['plan']['hotel_quotes']=lookup_hotel_quotes(self.service,self.event,t,refresh=True)
        tool._trip_read_reply(replace(self.event,event_id='read'),saved,[{'scope':'accommodation','source':'之前价格'}],False)
        self.assertEqual(self.transport.call_count,1)
        text='只看不存在门店的旧报价'
        referenced=replace(self.event,event_id='referenced',content=text,message_reference={'event_id':self.event.event_key})
        ir=IntentIR(1,'read',(IntentOperation('trip','check_prices',{'requested_checks':[
            {'scope':'accommodation','source':text,'names':['不存在门店']}]}),),source_spans=(text,))
        reply=tool.read_intent(referenced,None,ir,[],{},None,text)
        self.assertIn('未找到',reply);self.assertIn('不存在门店',reply);self.assertNotIn('399 CNY',reply)
        self.assertEqual(self.transport.call_count,1)


class QuoteWorkflowTests(WorkflowHarness,unittest.TestCase):
    def setup_quotes(self,*,budget=True,missing=False,formal=False):
        self.create()
        if formal:self.save(self.context()["confirmations"][0])
        raw,item=fixture();raw=raw.replace('测试城','武汉').replace('2030年1月7日','2030年10月1日').replace('2030年1月9日','2030年10月3日').replace('1月7日','10月1日').replace('1月8日','10月2日')
        item.update(target_id='qh',city='武汉',hotel_text=raw,stay_text='2030年10月1日入住，2030年10月3日退房，1间房，2名成人，无儿童。',
            check_in='2030-10-01',check_out='2030-10-03',price_header=HEADER.replace('1月7日','10月1日').replace('1月8日','10月2日'),
            nightly_prices=[{'date':'2030-10-01','amount':'399'},{'date':'2030-10-02','amount':'399'}])
        place={**TARGET['h'],'id':'qh','cityname':'武汉'}
        self.amap.search_places.return_value.append(place)
        self.transport=Mock(return_value=(raw.encode(),ENDPOINT,'text/markdown'))
        self.extraction=FakeClient([model_response({'offers':[item]}),model_response({'offers':[item]})])
        self.tool.hotel_quotes=HotelQuotes(self.web.store,WendaoClient('fixture',True,transport=self.transport),self.extraction,'test')
        text='2030年10月1日入住，2030年10月3日退房，1间房，2名成人，无儿童'
        values={**STAY,'check_in':'2030-10-01','check_out':'2030-10-03'}
        changes={'hotel_stay':{'value':values,'source':text,'field_sources':{k:text for k in values}}}
        if budget:
            text+='，酒店350-450元每间每晚'
            changes['budget']={'scope':'accommodation','value':'350-450','source':text,'unit_text':'元每间每晚'}
        if missing:
            text='酒店我想住云泽酒店'
            changes={'accommodation':{'value':'云泽酒店','source':text,'preferred_brands':['云泽'],'brand_mode':'required'}}
        self.patch(changes,text,'stay')
        self.assertEqual(self.transport.call_count,0 if missing else 1)
        if not missing:self.action(self.context()['confirmations'][0],'revalidate','adopt-hotel-material')
        return self.candidate()

    def test_new_stay_budget_recompute_revalidate_and_exact_save(self):
        first=self.setup_quotes()
        self.assertEqual(len(first['plan']['hotel_quotes']['offers']),1)
        self.assertEqual(first['plan']['hotel_ids'],['qh'])
        self.assertEqual(first['plan']['hotel_recommendations'][0]['price_status'],'reference_in_budget')
        self.assertEqual(first['plan']['hotel_quotes']['coverage']['comparable'],1)
        self.assertEqual(self.transport.call_count,1)
        self.patch({'budget':{'scope':'accommodation','value':'380以内','source':'住宿预算380以内','unit_text':'元/间/晚'}},'住宿预算380以内','lower')
        revised=self.candidate()
        self.assertEqual(revised['plan']['hotel_quotes'],first['plan']['hotel_quotes'])
        self.assertEqual(revised['plan']['hotel_ids'],[])
        self.assertEqual(self.transport.call_count,1)
        saved=self.save(self.context()['confirmations'][0])
        self.assertEqual(saved['plan']['hotel_quotes'],revised['plan']['hotel_quotes'])
        self.assertEqual(saved['plan']['hotel_ids'],[])
        self.assertEqual(self.transport.call_count,1)

    def test_refresh_saved_lookup_and_delete_keep_user_data_scoped(self):
        first=self.setup_quotes();self.save(self.context()['confirmations'][0]);formal=self.web.store.trips.list_for_owner(self.web.event(''))
        text='请刷新酒店入住日报价，只查价格，不修改行程'
        self.compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('trip','check_prices',{
            'requested_checks':[{'scope':'accommodation','source':text,'mode':'refresh'}]}),),source_spans=(text,))
        self.web.send(text,key='refresh');self.assertTrue(self.web.run_job())
        self.assertIn('399',self.web.messages()[-1]['content']);self.assertFalse(self.context()['confirmations'])
        self.assertEqual(self.web.store.trips.list_for_owner(self.web.event('')),formal)
        self.assertEqual(self.transport.call_count,2)
        before=len(self.web.store.research.hotel_quote_records(self.web.event('')))
        response=self.web.client.delete('/api/conversations/'+self.web.identity,headers=self.web.headers)
        self.assertEqual(response.status_code,202,response.text)
        from services.conversation_deletion import ConversationDeletionService
        cleanup=ConversationDeletionService(self.web.repo)
        self.assertEqual(cleanup.cleanup(self.web.identity)['state'],'completed')
        with self.web.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM wendao_requests WHERE scope=?',(self.web.identity,)).fetchone()[0],0)
        self.assertEqual(before,2)

    def test_readonly_new_price_is_adopted_only_by_a_later_modification_preview(self):
        before=self.setup_quotes()
        new_raw=self.transport.return_value[0].decode().replace('399元','499元').replace('798元','998元')
        self.transport.return_value=(new_raw.encode(),ENDPOINT,'text/markdown')
        payload=json.loads(self.extraction.completions.responses[0].choices[0].message.content)
        for item in payload['offers']:
            item['nightly_prices']=[{**n,'amount':'499'} for n in item['nightly_prices']];item['reported_total']='998'
            for key in ('hotel_text','price_row','terms_text'):item[key]=item[key].replace('399元','499元').replace('798元','998元')
        self.extraction.completions.responses[0]=model_response(payload)
        text='请刷新酒店房价，只查价不要修改'
        self.compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('trip','check_prices',{
            'requested_checks':[{'scope':'accommodation','source':text,'mode':'refresh'}]}),),source_spans=(text,))
        self.web.send(text,'new-price');self.assertTrue(self.web.run_job())
        self.assertEqual(self.candidate(),before)
        self.patch({'budget':{'scope':'accommodation','value':'450以内','source':'住宿预算450以内','unit_text':'元/间/晚'}},'住宿预算450以内','use-new-price')
        after=self.candidate();self.assertNotEqual(after['plan']['hotel_quotes']['request_id'],before['plan']['hotel_quotes']['request_id'])
        self.assertEqual(after['plan']['hotel_ids'],[]);self.assertEqual(self.transport.call_count,2)

    def test_legacy_no_budget_missing_conditions_continue_without_explicit_price_command(self):
        first=self.setup_quotes(budget=False,missing=True,formal=True)
        self.assertFalse(first['spec'].get('budgets',{}).get('accommodation'))
        self.assertIn('请一并补充',first['plan']['hotel_quotes']['note'])
        self.assertFalse(self.transport.called)
        formal=self.web.store.trips.list_for_owner(self.web.event(''))
        text='2030年10月1日入住，2030年10月3日退房，1间房，2名成人，无儿童'
        values={**STAY,'check_in':'2030-10-01','check_out':'2030-10-03'}
        reply=self.patch({'hotel_stay':{'value':values,'source':text,'field_sources':{k:text for k in values}}},text,'complete-stay')
        self.assertEqual(self.transport.call_count,1)
        self.assertTrue(self.candidate()['plan']['hotel_quotes']['offers'])
        self.assertFalse(self.candidate()['spec'].get('budgets',{}).get('accommodation'))
        self.assertEqual(self.web.store.trips.list_for_owner(self.web.event('')),formal)
        self.assertIn('399',reply)
        self.context();self.context()
        self.assertEqual(self.transport.call_count,1)

    def test_stale_quote_is_refreshed_for_an_unchanged_lodging_request(self):
        first=self.setup_quotes(budget=False)
        self.assertFalse(first['spec'].get('budgets',{}).get('accommodation'))
        self.tool.hotel_quotes.clock=lambda:datetime.fromisoformat(first['plan']['hotel_quotes']['retrieved_at'])+timedelta(minutes=31)
        text='酒店我想住云泽酒店'
        changes={'accommodation':{'value':'云泽酒店','source':text,'preferred_brands':['云泽'],'brand_mode':'required'}}
        self.patch(changes,text,'repeat-hotel')
        self.assertEqual(self.transport.call_count,2)
        self.assertNotEqual(self.candidate()['plan']['hotel_quotes']['request_id'],first['plan']['hotel_quotes']['request_id'])

    def test_reference_hotel_adoption_uses_existing_preview_and_confirm_transaction(self):
        quoted=self.setup_quotes(budget=False)
        self.save(self.context()['confirmations'][0])
        formal=self.web.store.trips.list_for_owner(self.web.event(''))
        event=replace(self.web.event('就用第一家','adopt-price'),message_reference={
            'event_id':quoted['plan']['hotel_quotes']['source_event_id']})
        claim=self.web.store.delivery.begin_event(event.event_key)
        ir=IntentIR(1,'update',(IntentOperation('trip','adopt_hotel_quote',{
            'reference':{'selection':'第一家','ordinal':1}}),),requires_confirmation=True,source_spans=('第一家',))
        reply=self.tool.adopt_hotel_quote(event,claim,ir)
        self.assertIn('预览',reply);self.assertIn('尚未',reply)
        self.assertEqual(self.web.store.trips.list_for_owner(self.web.event('')),formal)
        draft=self.web.store.trips.current_drafts(self.web.event(''))[0]
        candidate=draft.slots['candidate_trip']
        self.assertEqual(candidate['plan']['hotel_ids'],['qh']);self.assertEqual(candidate['spec']['hotel_preferences']['selected_poi_id'],'qh')
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(candidate['plan']['hotel_quotes']['request_id'],quoted['plan']['hotel_quotes']['request_id'])
