from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from infrastructure.web_reader import PageLinks
from services.official_sources import OfficialSources
from tools.research.official_links import (VERSION, allowed, current_link, navigation_key,
    navigation_url, priority, selected_link, verified_link)
from tools.research.evidence import presentation, render, validate_written, writing_messages
from tools.research.tool import ResearchTool, write_answer
from test_official_attraction_research import entry
from test_travel_answer import data as saved_data, proposal


def page(url, text, *, key=1, links=(), title=''):
    return {'id':key, 'url':url, 'requested_url':url, 'type':'navigation', 'status':'read',
            'text':text, 'title':title, 'links':list(links), 'content_hash':hashlib.sha256(text.encode()).hexdigest(),
            'retrieved_at':datetime.now(timezone.utc).isoformat()}


def identity_pages(name='测试博物馆', city='武汉'):
    site=page('https://museum.example/visit', name+'\n地址：'+city+'市测试路\n主办单位：'+name, key=2)
    authority=page('https://wlt.wuhan.gov.cn/article', city+'市文化和旅游局\n'+name+'官方网站',
        links=[{'url':site['url'], 'text':name+'官方网站'}])
    return authority,site


def navigation_data():
    value=saved_data()
    value.pop('answer_write',None)
    target=value.get('target_scope',{}).get('targets',value.get('provider_targets'))[0]
    city=value.get('target_scope',{}).get('city') or '南京'
    authority,landing=identity_pages(target['name'],city)
    authority['url']=authority['requested_url']='https://wlt.nanjing.gov.cn/article'
    start=max(s['id'] for s in value['sources'])+1
    authority['id'],landing['id']=start,start+1
    value['sources'].extend((authority,landing))
    link=verified_link({**target,'target_id':'target-1'},city,[authority,landing],OfficialSources([]),{})
    value['official_links']={'target-1':link}
    value['report']=render(presentation(value),detailed=True)
    return value


class NavigationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.target={'target_id':'target-1', 'name':'测试博物馆', 'visit_mode':'entry'}
        self.scope={'city':'武汉', 'requested_domains':[], 'excluded_domains':[], 'strict_sources':False}
        self.registry=OfficialSources([])

    def verify(self, pages, **kwargs):
        return verified_link(self.target, '武汉', pages, self.registry, self.scope, **kwargs)

    def test_explicit_government_outlink_and_operator_form_navigation_only(self):
        pages=identity_pages()
        link=self.verify(pages)
        self.assertEqual(link['status'], 'verified_navigation')
        self.assertEqual(link['identity_basis']['kind'], 'official_outlink')
        self.assertEqual(set(link['evidence_source_ids']), {1,2})
        self.assertEqual(self.registry.verify('测试博物馆',link['url'],'武汉')['status'], 'unverified')
        self.assertEqual(self.registry.entries, [])

    def test_reviewed_identity_keeps_exact_city_branch_and_path_scope(self):
        registry=OfficialSources([entry()])
        landing=page('https://museum.example/visitor/policy', '测试博物馆')
        record=verified_link(self.target,'武汉',[landing],registry,self.scope)
        self.assertEqual(record['identity_basis']['kind'], 'reviewed_registry')
        for city,url in (('南京',landing['url']),('武汉','https://museum.example/visitor-other')):
            self.assertIsNone(verified_link(self.target,city,[{**landing,'url':url}],registry,self.scope))

    def test_title_domain_filing_and_copyright_alone_are_not_proof(self):
        _,landing=identity_pages()
        landing['title']='测试博物馆官方网站'
        landing['text']='测试博物馆 武汉市'
        self.assertIsNone(self.verify([landing]))
        authority=page('https://travel.example/guide','武汉市文化和旅游局 测试博物馆官方网站',links=[{'url':landing['url'],'text':'测试博物馆官网'}])
        self.assertIsNone(self.verify([authority,landing]))

    def test_government_introduction_without_official_outlink_is_not_an_attraction_site(self):
        authority,landing=identity_pages()
        authority['links']=[]
        self.assertEqual(self.verify([authority,landing])['identity_basis']['kind'],'operator_declaration')
        authority['links']=[{'url':landing['url'],'text':'测试博物馆门票预订'}]
        self.assertEqual(self.verify([authority,landing])['url'],landing['url'])
        self.assertIsNone(self.verify([authority]))

    def test_wrong_city_branch_and_shared_operator_do_not_bind(self):
        authority,landing=identity_pages()
        for text in ('测试博物馆\n南京市测试路\n主办单位：测试博物馆',
                     '测试博物馆东馆\n武汉市测试路\n主办单位：测试博物馆东馆'):
            with self.subTest(text=text):
                self.assertIsNone(self.verify([authority,{**landing,'text':text}]))
        self.target['name']='测试博物馆东馆'
        self.assertIsNone(self.verify([authority,landing]))

    def test_ota_and_cross_host_redirects_are_rejected(self):
        authority,landing=identity_pages()
        ota={**landing,'url':'https://tickets.ctrip.com/visit','requested_url':'https://tickets.ctrip.com/visit'}
        authority['links']=[{'url':ota['url'],'text':'测试博物馆官方网站'}]
        self.assertIsNone(self.verify([authority,ota],blocked_domains=['ctrip.com']))
        self.assertIsNone(self.verify([authority,{**landing,'url':'https://foreign.example/visit'}]))

    def test_conflicting_independent_site_candidates_do_not_choose_first(self):
        authority,landing=identity_pages()
        other={**landing,'id':3,'url':'https://other.example/visit','requested_url':'https://other.example/visit'}
        authority['links'].append({'url':other['url'],'text':'测试博物馆官方网站'})
        self.assertIsNone(self.verify([authority,landing,other]))

    def test_literal_public_https_policy_and_source_filters(self):
        for url in ('https://127.0.0.1/a','https://10.0.0.1/a','https://localhost/a',
                    'https://museum.example/a?token=secret','javascript:alert(1)',
                    'https://user:pass@museum.example/a','https://museum.example/a\n'):
            self.assertFalse(navigation_url(url),url)
        for url in ('http://museum.example','https://museum.example/index.php?m=pc#visit'):
            self.assertTrue(navigation_url(url))
        self.assertFalse(allowed('https://museum.example/a',{**self.scope,'wendao_only':True}))
        self.assertFalse(allowed('https://museum.example/a',{**self.scope,'excluded_domains':['museum.example']}))
        self.assertFalse(allowed('https://museum.example/a',{**self.scope,'strict_sources':True,'requested_domains':['other.example']}))

    def test_cache_key_covers_entity_city_source_policy_and_strategy(self):
        original=navigation_key(self.target,'武汉',self.scope)
        self.assertNotEqual(original,navigation_key(self.target,'南京',self.scope))
        self.assertNotEqual(original,navigation_key({**self.target,'name':'测试博物馆东馆'},'武汉',self.scope))
        self.assertNotEqual(original,navigation_key(self.target,'武汉',{**self.scope,'excluded_domains':['other.example']}))
        registered=entry()
        self.assertNotEqual(navigation_key(self.target,'武汉',self.scope,registered),
            navigation_key(self.target,'武汉',self.scope,{**registered,'registry_version':'revised'}))
        link=self.verify(identity_pages())
        self.assertTrue(current_link(link))
        self.assertFalse(current_link({**link,'checked_at':(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()}))
        self.assertFalse(current_link({**link,'schema_version':'unknown'}))

    def test_opening_only_and_unreserved_outdoor_priority_are_distinct_from_paid_entry(self):
        self.assertIsNone(priority(self.target,{},['opening_hours'],'什么时候开放'))
        self.assertEqual(priority(self.target,{},['opening_hours'],'官网上的开放时间'),0)
        outdoor={**self.target,'name':'测试公园'}
        self.assertEqual(priority(outdoor,{'slots':[{'topic':'reservation_rules','text':'无需预约'}]},
                                  ['opening_hours','reservation_rules'],'介绍这些景点'),1)
        self.assertEqual(priority(self.target,{'slots':[{'topic':'ticket_price','text':'票价未知，高德人均50元'}]},
                                  ['ticket_price','reservation_rules'],'介绍这些景点'),0)

    def test_page_links_retains_local_official_anchor_context(self):
        links=PageLinks('https://wlt.wuhan.gov.cn/article')
        links.feed('<p>测试博物馆官方网站：<a href="https://museum.example/visit">进入官网</a></p>')
        self.assertIn('测试博物馆官方网站',links.links[0]['context'])


class NavigationWorkflowTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=MemoryStore(Path(folder.name)/'test.db')
        self.event=ChatEvent('onebot','group','nav','scope','owner','介绍方案中景点的门票与预约信息')
        self.value={'query':'武汉一日游','sources':[],'facts':[],'fact_schema':'attraction-facts-v1',
            'plan_title':'武汉方案','dates':{},'target_scope':{'city':'武汉','targets':[
                {'name':'测试博物馆','day_index':1,'visit_mode':'entry'}],
                'fields':{'topics':['opening_hours','ticket_price','reservation_rules']},
                'requested_domains':[],'excluded_domains':[],'strict_sources':False}}
        self.authority,self.landing=identity_pages()
        self.search=SimpleNamespace(configured=True,search=Mock(return_value=[{'url':self.authority['url']}]))
        self.reader=Mock(side_effect=lambda url,**kw:{k:deepcopy(v) for k,v in
            {p['url']:p for p in (self.authority,self.landing)}[url].items() if k!='id'})
        self.service=ResearchTool(self.store,self.search,reader=self.reader,official_sources=OfficialSources([]))

    def discover(self,available=lambda:True):
        self.service.search_tool.discover_links(self.event,self.value,available)
        return self.store.research.get(self.event)

    def test_navigation_is_independent_of_fact_gaps_and_does_not_run_models_or_promote_facts(self):
        old=deepcopy(self.value['facts'])
        self.discover()
        self.assertEqual(self.search.search.call_count,1)
        self.assertEqual(self.reader.call_count,2)
        self.assertEqual(self.value['facts'],old)
        self.assertTrue(all(s['official_identity']['status']=='unverified' for s in self.value['sources']))
        self.assertEqual(len(self.value['official_links']),1)
        self.assertIn('官方网站',self.search.search.call_args.args[0])
        self.assertNotIn('开放时间',self.search.search.call_args.args[0])

    def test_repeat_targets_and_completed_recovery_reuse_one_discovery(self):
        self.value['target_scope']['targets']*=2
        self.discover();self.discover()
        self.assertEqual(self.search.search.call_count,1)
        self.assertEqual(self.reader.call_count,2)
        self.assertEqual(set(self.value['official_links']),{'target-1','target-2'})

    def test_saved_link_reuse_is_owner_scoped_and_rebinds_source_ids(self):
        self.discover()
        self.event=ChatEvent('onebot','group','next','scope','owner','介绍方案中景点的门票与预约信息')
        self.value=deepcopy(self.value)
        for key in ('official_links','official_link_progress','official_link_scope','request_counts'):
            self.value.pop(key,None)
        self.value['sources']=[]
        self.discover()
        self.assertEqual(self.search.search.call_count,1)
        self.assertIsNotNone(selected_link(self.value,self.value['target_scope']['targets'][0],'target-1'))
        foreign=ChatEvent('onebot','group','read','scope','other','读取')
        self.assertEqual(self.store.research.navigation_cache(foreign),{})

    def test_unknown_search_submission_is_checkpointed_and_not_reissued(self):
        self.search.search.side_effect=KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):self.discover()
        self.value=self.store.research.get(self.event)
        self.search.search.side_effect=None
        self.discover()
        self.assertEqual(self.search.search.call_count,1)
        self.assertEqual(self.value['request_counts']['search'],1)

    def test_existing_official_outlink_is_read_without_an_extra_search(self):
        self.value['sources']=[deepcopy(self.authority)]
        self.discover()
        self.search.search.assert_not_called()
        self.assertEqual(self.reader.call_count,1)
        self.assertEqual(len(self.value['official_links']),1)

    def test_operator_candidate_gets_at_most_one_independent_identity_followup(self):
        self.search.search.side_effect=[[{'url':self.landing['url']}],[{'url':self.authority['url']}]]
        self.discover()
        self.assertEqual(self.search.search.call_count,1)
        self.assertEqual(self.reader.call_count,1)
        self.assertEqual(len(self.value['official_links']),1)

    def test_source_domain_limits_apply_before_reading_search_results(self):
        self.value['target_scope'].update(strict_sources=True,requested_domains=['only.example'])
        self.discover()
        self.assertEqual(self.search.search.call_args.kwargs['include_domains'],['only.example'])
        self.reader.assert_not_called()
        self.assertEqual(self.value['official_links'],{})

    def test_page_budget_and_expired_identity_cache_do_not_create_unbounded_work(self):
        self.value['request_counts']={'pages':12,'search':0}
        self.discover()
        self.search.search.assert_not_called();self.reader.assert_not_called()
        progress=next(iter(self.value['official_link_progress'].values()))
        self.assertEqual(progress['reason'],'not_attempted')
        self.assertEqual(progress['stop_reason'],'page_budget_exhausted')
        self.value.pop('request_counts');self.value.pop('official_link_progress')
        self.discover()
        link=self.value['official_links']['target-1']
        link['checked_at']=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
        self.store.research.save(self.event,'partial',self.value)
        self.event=ChatEvent('onebot','group','expired-cache','scope','owner','介绍景点')
        for key in ('official_links','official_link_progress','request_counts'):
            self.value.pop(key,None)
        self.value['sources']=[]
        self.discover()
        self.assertEqual(self.search.search.call_count,2)

    def test_all_source_restrictions_and_disabled_discovery_preserve_the_answer(self):
        for text in ('不要联网','只看已保存资料','只用问道','只看高德'):
            self.event=ChatEvent('onebot','group',text,'scope','owner',text)
            self.discover()
        self.search.search.assert_not_called();self.reader.assert_not_called()
        self.service.search_tool.enabled=False
        self.event=ChatEvent('onebot','group','off','scope','owner','介绍所有景点')
        self.discover()
        self.search.search.assert_not_called()

    def test_missing_search_key_empty_identity_and_time_budget_are_nonfatal(self):
        self.search.configured=False
        self.discover();self.assertEqual(self.value['official_links'],{})
        self.search.search.assert_not_called()
        self.value.pop('official_link_progress');self.search.configured=True
        self.discover(available=lambda:False)
        self.search.search.assert_not_called()

    def test_twelve_targets_share_a_search_cap_instead_of_two_queries_each(self):
        self.value['target_scope']['targets']=[{'name':f'测试场馆{i}','visit_mode':'entry'} for i in range(12)]
        self.search.search.return_value=[]
        self.discover()
        self.assertEqual(self.search.search.call_count,8)
        self.assertEqual(self.value['request_counts']['search'],8)
        self.assertFalse(self.value['official_links'])

    def test_final_redirect_is_rejected_and_no_policy_facts_are_added(self):
        self.landing['url']='https://foreign.example/visit'
        self.reader.side_effect=lambda url,**kw:deepcopy(self.authority if url==self.authority['url'] else self.landing)
        self.discover()
        self.assertEqual(self.value['official_links'],{})
        self.assertEqual(self.value['facts'],[])


class NavigationPresentationTests(unittest.TestCase):
    def setUp(self):
        self.value=navigation_data()
        self.link=self.value['official_links']['target-1']
        self.assertIsNotNone(self.link)

    def test_model_does_not_receive_or_control_the_url_and_navigation_does_not_verify_prices(self):
        before=presentation({**self.value,'official_links':{}})
        base=presentation(self.value)
        self.assertEqual(base['entries'][0]['practical'],before['entries'][0]['practical'])
        messages,supplied=writing_messages(base,'介绍景点')
        self.assertNotIn(self.link['url'],messages[1]['content'])
        request=proposal(base)
        request['entries'][0]['official_link']={'url':'https://fake.example'}
        result=validate_written(request,base,supplied)
        self.assertEqual(result['entries'][0]['official_link'],self.link)
        text=render(result)
        self.assertIn(self.link['url'],text)
        self.assertNotIn('https://fake.example',text)
        self.assertLess(text.index('**预约规则：**'),text.index(self.link['url']))

    def test_saved_results_and_restricted_saved_materials_use_the_same_projection(self):
        self.value['answer_write']={'result':presentation(self.value)}
        self.assertIn(self.link['url'],render(presentation(self.value)))
        restricted={**self.value,'provider_policy':{'only':True}}
        self.assertNotIn(self.link['url'],render(presentation(restricted)))
        restricted={**self.value,'official_link_display_scope':{'strict_sources':True,'requested_domains':['museum.example']}}
        self.assertNotIn(self.link['url'],render(presentation(restricted)))

    def test_chat_core_budget_keeps_complete_links_and_report_keeps_all_conditions(self):
        view=presentation(self.value)
        view['entries'][0]['intro']={'text':'文化介绍。'*2000}
        text=render(view)
        self.assertLessEqual(len(text),6000)
        self.assertIn('](<'+self.link['url']+'>)',text)
        for entry in view['entries']:
            for slot in entry['practical']:slot['text']='完整条件。'*200
        compact=render(view,max_chars=1500)
        self.assertLessEqual(len(compact),1500)
        self.assertIn(self.link['url'],compact)
        self.assertIn(self.link['url'],render(view,detailed=True))

    def test_legacy_record_without_navigation_has_no_placeholder(self):
        self.value.pop('official_links')
        text=render(presentation(self.value))
        self.assertNotIn('详细信息见景点官网',text)
        self.assertNotIn('官网未找到',text)
