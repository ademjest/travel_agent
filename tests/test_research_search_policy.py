import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agents.research_search_planner import ResearchSearchPlanner, SKILL_DIR
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from infrastructure.public_http import PublicHTTPError
from infrastructure.search_client import SearchClient
from infrastructure.web_reader import read_page
from services.research_service import ResearchService
from test_travel_agent import FakeClient, assistant_message, completion


def plan_value(region='china', **kwargs):
    destination='武汉' if region!='international' else '京都'
    return {'region':region,'destination':destination,'query':'武汉 三日游 攻略','official_query':'武汉 官方 旅游 信息',
            'requested_domains':[],'excluded_domains':[],'strict_sources':False,'platform_request':'',**kwargs}


def planner(value):
    client=FakeClient([completion(assistant_message(content=json.dumps(value,ensure_ascii=False)))])
    return ResearchSearchPlanner(client,'test'),client


class SearchPolicyTests(unittest.TestCase):
    def test_domestic_queries_use_community_and_travel_filters(self):
        service,client=planner(plan_value())
        plan=service.plan('帮我搜索武汉三日游攻略')
        self.assertIn('xiaohongshu.com',plan['batches'][0]['domains'])
        self.assertIn('zhihu.com',plan['batches'][0]['domains'])
        self.assertIn('ctrip.com',plan['batches'][1]['domains'])
        self.assertEqual(plan['batches'][2]['domains'],[])
        self.assertIn('旅行攻略选源',client.completions.requests[0]['messages'][0]['content'])

    def test_chinese_language_overseas_request_uses_international_sources(self):
        service,_=planner(plan_value('international',query='Kyoto three day travel itinerary',official_query='Kyoto official tourism'))
        plan=service.plan('帮我搜索京都三日游攻略')
        self.assertIn('tripadvisor.com',plan['batches'][0]['domains'])
        self.assertFalse(any('xiaohongshu.com' in b['domains'] for b in plan['batches']))
        self.assertEqual(plan['batches'][-1]['query'],'Kyoto official tourism')

    def test_explicit_strict_platform_overrides_region_and_prevents_fallback(self):
        service,_=planner(plan_value('international',requested_domains=['xiaohongshu.com'],strict_sources=True,
            platform_request='只看小红书'))
        result=service.plan('京都攻略只看小红书')
        self.assertEqual(len(result['batches']),1)
        self.assertEqual(result['batches'][0]['domains'],['xiaohongshu.com'])
        self.assertTrue(result['strict_sources'])

    def test_explicit_exclusion_is_preserved_in_every_batch(self):
        service,_=planner(plan_value(excluded_domains=['xiaohongshu.com'],platform_request='不要小红书'))
        result=service.plan('武汉攻略，不要小红书')
        self.assertTrue(all('xiaohongshu.com' not in b['domains'] for b in result['batches']))
        self.assertEqual(result['excluded_domains'],['xiaohongshu.com'])

    def test_failed_parsing_cannot_ignore_platform_restriction(self):
        service,_=planner({'invalid':True})
        result=service.plan('只看知乎的武汉攻略')
        self.assertEqual(result['batches'][0]['domains'],['zhihu.com'])
        self.assertTrue(result['strict_sources'])

    def test_skill_configuration_changes_are_read_on_next_search(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'SKILL.md').write_text((SKILL_DIR/'SKILL.md').read_text(encoding='utf-8'),encoding='utf-8')
            data=json.loads((SKILL_DIR/'sources.json').read_text(encoding='utf-8'))
            data['groups']['china_travel']=['qyer.com']
            (root/'sources.json').write_text(json.dumps(data),encoding='utf-8')
            service,_=planner(plan_value())
            service.skill_dir=root
            self.assertEqual(service.plan('武汉攻略')['batches'][1]['domains'],['qyer.com'])

    def test_search_client_transmits_real_filters_and_rejects_invalid_domain(self):
        with patch('infrastructure.search_client.request_public',return_value=(b'{"results":[]}', '', 'application/json')) as request:
            SearchClient('fixture').search('武汉攻略',include_domains=['zhihu.com'],exclude_domains=['xiaohongshu.com'])
            payload=request.call_args.kwargs['payload']
            self.assertEqual(payload['include_domains'],['zhihu.com'])
            self.assertEqual(payload['include_domains_mode'],'restrict')
            self.assertEqual(payload['exclude_domains'],['xiaohongshu.com'])
            SearchClient('fixture').search('武汉攻略',exclude_domains=['xiaohongshu.com'])
            self.assertNotIn('include_domains_mode',request.call_args.kwargs['payload'])
            with self.assertRaises(ValueError):
                SearchClient('fixture').search('test',include_domains=['https://localhost/private'])

    def test_login_and_empty_js_shell_are_not_treated_as_read_articles(self):
        for body in ('<title>安全验证</title><p>请完成验证</p>', '<title>旅行笔记</title><div id="app"></div><script>load()</script>'):
            with patch('infrastructure.web_reader.request_public',return_value=(body.encode(),'https://example.test','text/html')):
                with self.assertRaises(PublicHTTPError): read_page('https://example.test')
        body='<title>武汉游记</title><article><p>'+('公开旅行经验与行程建议。'*20)+'</p></article>'
        with patch('infrastructure.web_reader.request_public',return_value=(body.encode(),'https://example.test','text/html')):
            self.assertEqual(read_page('https://example.test')['status'],'read')

    def test_http_200_soft_404_with_long_footer_is_not_an_article(self):
        body='<title>小红书 - 你访问的页面不见了</title><p>'+('公司地址及网站导航。'*30)+'</p>'
        with patch('infrastructure.web_reader.request_public',return_value=(body.encode(),'https://www.xiaohongshu.com/explore/example','text/html')):
            with self.assertRaises(PublicHTTPError) as error:
                read_page('https://www.xiaohongshu.com/explore/example')
        self.assertEqual(error.exception.code,'page_unavailable')

    def test_navigation_and_footer_are_not_public_article_content(self):
        body='<title>旅行社区</title><nav>搜索<br>发现<br>RED<br>直播<br>发布<br>通知<br>消息</nav><footer>© 2026 '+('网站备案与公司信息。'*30)+'</footer>'
        with patch('infrastructure.web_reader.request_public',return_value=(body.encode(),'https://example.test','text/html')):
            with self.assertRaises(PublicHTTPError): read_page('https://example.test')


class ResearchExecutionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store=MemoryStore(Path(temp.name)/'test.db')
        self.search=Mock(configured=True)
        self.reader=Mock(side_effect=lambda url:{'text':'真实公开文章','status':'read','content_hash':url})
        self.service=ResearchService(self.store,self.search,reader=self.reader)
        self.service.search_planner=planner(plan_value())[0]
        self.event=ChatEvent('web','group','query','test','owner','帮我搜索武汉三日游攻略')

    def test_filters_diversity_and_checkpoint_are_applied(self):
        def search(query,**kwargs):
            domain=kwargs.get('include_domains',['tourism.example'])[0]
            return [{'url':f'https://{domain}/wuhan','title':'武汉攻略','snippet':'武汉旅行经验'}]
        self.search.search.side_effect=search
        reply=self.service.handle(self.event,self.store.begin_event(self.event.event_key))
        self.assertIn('优先中文社区',reply)
        self.assertEqual(self.search.search.call_count,3)
        self.assertIn('include_domains',self.search.search.call_args_list[0].kwargs)
        self.service.handle(self.event,None)
        self.assertEqual(self.search.search.call_count,3)

    def test_related_followup_inherits_conversation_source_preference(self):
        first_plan=plan_value(destination='开封',requested_domains=['xiaohongshu.com'],platform_request='小红书')
        self.service.search_planner=planner(first_plan)[0]
        first=ChatEvent('web','group','first','test','owner','搜索开封攻略，参考小红书')
        self.search.search.return_value=[]
        self.service.handle(first,None)
        context=self.store.research.context(first)
        self.assertEqual(context['preferred_domains'],['xiaohongshu.com'])
        second_plan=plan_value(destination='开封',query='开封 四日游 攻略',official_query='开封 官方旅游')
        self.service.search_planner=planner(second_plan)[0]
        second=ChatEvent('web','group','second','test','owner','帮我搜索开封四日游攻略')
        self.service.handle(second,None)
        data=self.store.research.get(second)
        self.assertEqual(data['search_plan']['batches'][0]['domains'],['xiaohongshu.com'])
        self.assertIn('继续优先',data['search_plan']['note'])

    def test_different_destination_does_not_inherit_source_preference(self):
        first=ChatEvent('web','group','first-city','test','owner','搜索开封攻略，参考小红书')
        self.store.research.update_context(first,{**plan_value(destination='开封'), 'requested_domains':['xiaohongshu.com']})
        tokyo=plan_value('international',destination='东京',query='Tokyo travel guide',official_query='Tokyo official tourism')
        self.service.search_planner=planner(tokyo)[0]
        second=ChatEvent('web','group','tokyo','test','owner','搜索东京攻略')
        self.search.search.return_value=[]
        self.service.handle(second,None)
        self.assertIn('tripadvisor.com',self.store.research.get(second)['search_plan']['batches'][0]['domains'])

    def test_checkpoint_resume_does_not_repeat_successful_search_batches(self):
        calls=[]
        def search(query,**kwargs):
            domain=kwargs.get('include_domains',['tourism.example'])[0]
            calls.append(domain)
            if len(calls)==2: raise RuntimeError('simulated worker interruption')
            return [{'url':f'https://{domain}/wuhan','title':'武汉攻略','snippet':'武汉旅行经验'}]
        self.search.search.side_effect=search
        with self.assertRaises(RuntimeError): self.service.handle(self.event,None)
        self.service.handle(self.event,None)
        self.assertEqual(calls.count('xiaohongshu.com'),1)
        self.assertEqual(calls.count('mafengwo.cn'),2)

    def test_strict_site_has_no_cross_platform_fallback_after_blocked_body(self):
        self.service.search_planner=planner(plan_value(requested_domains=['zhihu.com'],strict_sources=True,
            platform_request='只看知乎'))[0]
        self.event=ChatEvent('web','group','strict','test','owner','搜索武汉攻略，只看知乎')
        self.search.search.return_value=[{'url':'https://www.zhihu.com/question/1','title':'武汉经验','snippet':'仅公开搜索摘要'}]
        self.reader.side_effect=PublicHTTPError('forbidden')
        reply=self.service.handle(self.event,None)
        self.assertIn('仅搜索摘要',reply)
        self.assertEqual(self.search.search.call_count,1)

    def test_error_page_search_snippet_is_not_used_as_travel_evidence(self):
        self.service.search_planner=planner(plan_value(requested_domains=['xiaohongshu.com'],strict_sources=True,
            platform_request='只看小红书'))[0]
        self.event=ChatEvent('web','group','missing','test','owner','搜索武汉攻略，只看小红书')
        self.search.search.return_value=[{'url':'https://www.xiaohongshu.com/explore/example','title':'小红书',
            'snippet':'小红书 - 你访问的页面不见了\n公司地址和导航'}]
        self.reader.side_effect=PublicHTTPError('page_unavailable')
        reply=self.service.handle(self.event,None)
        source=self.store.research.get(self.event)['sources'][0]
        self.assertEqual(source['status'],'unavailable')
        self.assertEqual(source['text'],'')
        self.assertIn('未取得可用内容',reply)

    def test_navigation_only_search_snippet_is_not_evidence(self):
        self.service.search_planner=planner(plan_value(requested_domains=['xiaohongshu.com'],strict_sources=True,
            platform_request='只看小红书'))[0]
        self.event=ChatEvent('web','group','navigation','test','owner','搜索武汉攻略，只看小红书')
        self.search.search.return_value=[{'url':'https://www.xiaohongshu.com/explore/example','title':'小红书',
            'snippet':'搜索\n发现\nRED\n直播\n发布\n通知\n消息\n© 2026\n网站备案及公司信息'}]
        self.reader.side_effect=PublicHTTPError('page_unavailable')
        reply=self.service.handle(self.event,None)
        source=self.store.research.get(self.event)['sources'][0]
        self.assertEqual(source['status'],'unavailable')
        self.assertIn('未取得可用内容',reply)

    def test_priority_shortfall_uses_one_bounded_fallback(self):
        self.search.search.side_effect=[[],[],[],[{'url':'https://public.example/wuhan','title':'武汉攻略','snippet':'公开摘要'}]]
        reply=self.service.handle(self.event,None)
        self.assertIn('已补充其他公开检索结果',reply)
        self.assertEqual(self.search.search.call_count,4)

    def test_unreadable_priority_pages_trigger_one_public_fallback(self):
        def search(query,**kwargs):
            domain=kwargs.get('include_domains',['blocked.example'])[0]
            if self.search.search.call_count==4: domain='public.example'
            return [{'url':f'https://{domain}/wuhan','title':'武汉攻略','snippet':'公开搜索摘要'}]
        self.search.search.side_effect=search
        def read(url):
            if 'public.example' not in url: raise PublicHTTPError('forbidden')
            return {'text':'公开可读正文','status':'read','content_hash':'public'}
        self.reader.side_effect=read
        reply=self.service.handle(self.event,None)
        self.assertIn('已补充其他公开检索结果',reply)
        self.assertEqual(self.search.search.call_count,4)
        self.assertIn('public.example',reply)
        self.service.handle(self.event,None)
        self.assertEqual(self.search.search.call_count,4)

    def test_domain_matching_does_not_accept_spoofed_suffix(self):
        self.assertFalse(self.service._domain_allowed('https://zhihu.com.evil.test/a',['zhihu.com'],[]))
        self.assertTrue(self.service._domain_allowed('https://zhuanlan.zhihu.com/a',['zhihu.com'],[]))

    def test_resume_after_fallback_checkpoint_reads_saved_results_without_researching(self):
        def search(query,**kwargs):
            domain=kwargs.get('include_domains',['blocked.example'])[0]
            if self.search.search.call_count==4: domain='public.example'
            return [{'url':f'https://{domain}/wuhan','title':'武汉攻略','snippet':'搜索摘要'}]
        def read(url):
            if 'public.example' not in url: raise PublicHTTPError('forbidden')
            return {'text':'公开正文','status':'read','content_hash':'fallback'}
        self.search.search.side_effect=search
        self.reader.side_effect=read
        save=self.store.research.save
        def interrupt(event,stage,data):
            save(event,stage,data)
            if stage=='searching' and data.get('fallback_done'):
                raise RuntimeError('interrupted after fallback checkpoint')
        with patch.object(self.store.research,'save',side_effect=interrupt):
            with self.assertRaises(RuntimeError): self.service.handle(self.event,None)
        self.service.handle(self.event,None)
        self.assertEqual(self.search.search.call_count,4)
        self.assertTrue(any(s['status']=='read' and 'public.example' in s['url'] for s in self.store.research.get(self.event)['sources']))
