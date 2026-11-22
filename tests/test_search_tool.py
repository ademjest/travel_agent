from dataclasses import replace
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from app.request_router import RequestRouter
from core.chat_transport import ChatEvent
from core.intent_ir import IntentIR, IntentOperation, parse_intent_ir
from core.tool_result import AgentToolContext
from infrastructure.execution_diagnostics import SearchTrace
from infrastructure.memory_store import MemoryStore
from infrastructure.search_client import SearchClient
from infrastructure.web_reader import read_page
from services.official_sources import OfficialSources
from tools.search import SearchTool
from tools.registry import AgentToolRouter, catalog_schemas
from test_official_links import identity_pages


class StandaloneWebsiteTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.store=MemoryStore(self.root/'db.sqlite')
        self.event=ChatEvent('onebot','group','website','scope','owner','帮我查测试博物馆的官方网站链接')
        authority,landing=identity_pages()
        pages={p['url']:p for p in (authority,landing)}
        self.search=Mock(configured=True,search=Mock(return_value=[{'url':authority['url'],'title':'机构官网'}]))
        self.reader=Mock(side_effect=lambda url,**kwargs:{k:v for k,v in pages[url].items() if k!='id'})
        self.tool=SearchTool(self.store,self.search,reader=self.reader,registry=OfficialSources([]))
        self.router=AgentToolRouter(Mock(),Mock(),search_tool=self.tool)

    def context(self,event=None):
        e=event or self.event
        return AgentToolContext(e.platform,e.scope_id,e.sender_id,e.event_key,
            allowed_actions=frozenset({'query_official_website'}),event=e)

    def test_standalone_and_replay_need_no_trip_provider_or_model(self):
        first=self.router.execute_result('query_official_website',{'name':'测试博物馆'},self.context())
        self.assertIn('museum.example',first.data)
        self.assertEqual(first.status,'completed')
        calls=(self.search.search.call_count,self.reader.call_count)
        second=self.router.execute_result('query_official_website',{'name':'测试博物馆'},self.context())
        self.assertEqual(second.data,first.data)
        self.assertEqual(calls,(self.search.search.call_count,self.reader.call_count))

    def test_natural_language_router_uses_registered_search_action(self):
        ir=IntentIR(1,'read',(IntentOperation('search','query_official_website',{'name':'测试博物馆'}),),source_spans=('测试博物馆',))
        research=Mock()
        router=RequestRouter(self.store,Mock(try_compile=Mock(return_value=ir)),tool_executor=self.router.execute_result,research_service=research)
        event=replace(self.event,content='搜索测试博物馆的官网')
        claim=self.store.delivery.begin_event(event.event_key)
        reply,policy=router.route(event,claim)
        self.assertIn('museum.example',reply)
        research.handle.assert_not_called()

    def test_cross_workflow_page_reuse_does_not_spend_another_read(self):
        authority,_=identity_pages()
        data={'sources':[],'request_counts':{'pages':0,'search':0}}
        scope={'city':'武汉','requested_domains':[],'excluded_domains':[],'strict_sources':False}
        for kind in ('web','navigation'):
            self.tool.read_source(self.event,data,authority['url'],{'name':'测试博物馆'},None,scope,
                {'page_ids':[]},lambda:True,source_type=kind)
        self.assertEqual(self.reader.call_count,1)
        self.assertEqual(data['request_counts']['pages'],1)

    def test_multi_target_round_gives_more_than_first_entity_a_read_chance(self):
        self.search.search.side_effect=lambda query,**kwargs:[{'url':f'https://other.example/{hashlib.sha256(query.encode()).hexdigest()[:8]}/{i}'} for i in range(5)]
        self.reader.side_effect=lambda url,**kwargs:{'url':url,'status':'read','text':'其他内容','content_hash':url}
        data={'sources':[],'target_scope':{'city':'武汉','targets':[{'name':f'场馆{i}','visit_mode':'entry'} for i in range(12)]}}
        self.tool.discover_links(self.event,data,lambda:True)
        progress=list(data['official_link_progress'].values())
        self.assertGreaterEqual(sum(bool(p['page_ids']) for p in progress),6)
        self.assertLessEqual(data['request_counts']['pages'],12)
        initial=[call.args[0].rsplit('/',1)[0] for call in self.reader.call_args_list[:8]]
        self.assertGreaterEqual(len(set(initial)),6)

    def test_both_catalogs_expose_website(self):
        for version in ('legacy-v1','compact-v1'):
            self.assertTrue(any(s['function']['name']=='query_official_website' for s in catalog_schemas(catalog_version=version)))

    def test_continue_saved_navigation_respects_new_source_limit(self):
        self.router.execute_result('query_official_website',{'name':'测试博物馆'},self.context())
        calls=self.search.search.call_count
        event=replace(self.event,event_id='continue',content='只用问道，继续查询剩余官网')
        ctx=replace(self.context(event),allowed_actions=frozenset({'continue_official_websites'}))
        result=self.router.execute_result('continue_official_websites',{},ctx)
        self.assertNotIn('museum.example',result.data)
        self.assertEqual(self.search.search.call_count,calls)

    def test_continue_without_previous_research_requests_only_needed_input(self):
        ctx=replace(self.context(),allowed_actions=frozenset({'continue_official_websites'}))
        result=self.router.execute_result('continue_official_websites',{},ctx)
        self.assertEqual(result.status,'needs_input')
        self.assertEqual(result.missing_fields,('name',))

    def test_continue_keeps_confirmed_link_without_another_search(self):
        self.router.execute_result('query_official_website',{'name':'测试博物馆'},self.context())
        count=self.search.search.call_count
        event=replace(self.event,event_id='continue',content='继续查询剩余官网')
        ctx=replace(self.context(event),allowed_actions=frozenset({'continue_official_websites'}))
        result=self.router.execute_result('continue_official_websites',{},ctx)
        self.assertEqual(result.status,'completed')
        self.assertIn('museum.example',result.data)
        self.assertEqual(self.search.search.call_count,count)

    def test_wrong_scope_and_source_restriction(self):
        wrong=replace(self.context(),creator_id='someone_else')
        self.assertEqual(self.router.execute_result('query_official_website',{'name':'测试博物馆'},wrong).error_code,'wrong_resource')
        event=replace(self.event,event_id='offline',content='只用问道，查询测试博物馆的官网')
        result=self.router.execute_result('query_official_website',{'name':'测试博物馆'},self.context(event))
        self.assertNotIn('museum.example',result.data)
        self.search.search.assert_not_called()

    def test_website_schema_rejects_invented_city(self):
        raw={'schema_version':1,'action':'read','operations':[{'domain':'search','operation':'query_official_website','name':'测试博物馆','city':'武汉'}],
             'source_spans':['测试博物馆']}
        with self.assertRaises(ValueError):parse_intent_ir(raw,self.event.content)

    def test_malformed_provider_url_is_traced_without_reading(self):
        self.search.search.return_value=[{'url':'https://[bad','title':'官网'}]
        result=self.router.execute_result('query_official_website',{'name':'测试博物馆'},self.context())
        self.assertNotEqual(result.status,'completed')
        self.reader.assert_not_called()
        self.assertEqual(result.payload['progress']['candidate_trace'][0]['stage'],'filtered')

    def test_named_source_limit_applies_to_standalone_query(self):
        event=replace(self.event,content='只看小红书，查测试博物馆的官方网站')
        result=self.router.execute_result('query_official_website',{'name':'测试博物馆'},self.context(event))
        self.assertNotIn('museum.example',result.data)
        self.reader.assert_not_called()
        self.assertEqual(self.search.search.call_args.kwargs['include_domains'],['xiaohongshu.com'])


class SearchCaptureAndRedirectTests(unittest.TestCase):
    def test_invalid_read_contract_is_repaired_once_before_execution(self):
        from agents.intent_compiler import IntentCompiler
        text='查询测试博物馆官网'
        values=[{'domain':'places','operation':'search','website':'测试博物馆'},
                {'domain':'search','operation':'query_official_website','name':'测试博物馆'}]
        responses=[SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
            {'schema_version':1,'action':'read','operations':[v],'source_spans':['测试博物馆']},ensure_ascii=False)))]) for v in values]
        create=Mock(side_effect=responses)
        compiler=IntentCompiler(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),'model')
        ir=compiler.compile(text)
        self.assertEqual(ir.operations[0].domain,'search')
        self.assertEqual(create.call_count,2)

    def test_full_provider_response_survives_normalization_limits(self):
        body=json.dumps({'results':[{'url':f'https://museum.example/{i}','content':'a'*3000,'score':0.5} for i in range(6)]}).encode()
        with tempfile.TemporaryDirectory() as tmp, SearchTrace(Path(tmp)/'trace') as trace:
            with patch('infrastructure.search_client.request_public',return_value=(body,'https://api.tavily.com/search','application/json')):
                rows=SearchClient('test-key').search('测试博物馆 官网')
            saved=json.loads((trace.root/'search-results.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(len(saved['raw']['results']),6)
            self.assertEqual(len(saved['raw']['results'][0]['content']),3000)
            self.assertEqual(len(rows),5)
            self.assertNotIn('test-key',(trace.root/'search-results.jsonl').read_text(encoding='utf-8'))

    def test_static_government_redirect_reaches_article(self):
        shell=b"<script>window.location.href='./201807/article.html';\n//window.open('ignored');\n</script>"
        article=('<html><title>政府介绍</title><p>'+'真实场馆介绍及官网入口。'*20+'</p></html>').encode()
        with patch('infrastructure.web_reader.request_public',side_effect=[(shell,'https://wlj.example/museum/','text/html'),(article,'https://wlj.example/museum/201807/article.html','text/html')]) as fetch:
            page=read_page('https://wlj.example/museum/')
            self.assertEqual(page['status'],'read')
            self.assertEqual(fetch.call_args.args[0],'https://wlj.example/museum/201807/article.html')

    def test_static_redirect_cannot_cross_source_policy(self):
        shell=b'<meta http-equiv="refresh" content="0;url=https://unrelated.example/">'
        with patch('infrastructure.web_reader.request_public',return_value=(shell,'https://museum.example/','text/html')):
            with self.assertRaises(ValueError):read_page('https://museum.example/',url_allowed=lambda u:u.startswith('https://museum.example/'))

    def test_physical_http_budget_is_checked_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp, SearchTrace(Path(tmp)/'trace',max_requests=1) as trace:
            trace.before_request('https://museum.example/','GET')
            with self.assertRaises(RuntimeError):trace.before_request('https://museum.example/','GET')
            self.assertEqual(trace.requests,1)
