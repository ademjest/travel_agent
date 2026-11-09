import unittest
from unittest.mock import Mock
from core.tool_result import AgentToolContext
from infrastructure.public_http import PublicHTTPError
from infrastructure.public_http import request_public
from unittest.mock import patch
import test_search_tool
from tools.research.official_links import selected_link
from services.official_sources import OfficialSources
from test_official_attraction_research import entry


class RelaxedNavigationTests(unittest.TestCase):
    def setUp(self):
        test_search_tool.StandaloneWebsiteTests.setUp(self)

    def test_search_match_survives_forbidden_without_claiming_verified_rules(self):
        self.search.search.return_value=[{'url':'http://museum.example/index.php?m=pc',
            'title':'测试博物馆官方网站','snippet':'地址：武汉市'}]
        self.reader.side_effect=PublicHTTPError('forbidden')
        result=self.tool.execute_result('query_official_website',{'name':'测试博物馆','city':'武汉'},
            AgentToolContext(self.event.platform,self.event.scope_id,self.event.sender_id,self.event.event_key,
                event=self.event,execution_data={'scope':{'city':'武汉'}}))
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.payload['official_link']['status'],'candidate_navigation')
        self.assertIn('搜索匹配',result.data)
        saved=self.store.research.get(self.event)
        self.assertIsNotNone(selected_link(saved,{'name':'测试博物馆'},'target-1'))
        self.assertFalse(saved.get('facts'))

    def test_no_budget_does_not_dispatch_failed_tools(self):
        self.tool.executor=Mock()
        self.tool.discover_links(self.event,{'sources':[], 'provider_targets':[{'name':'测试馆'}]},lambda:False)
        self.tool.executor.assert_not_called()

    def test_registered_identity_survives_unreadable_page(self):
        self.tool.registry=OfficialSources([entry()])
        self.reader.side_effect=PublicHTTPError('forbidden')
        result=self.tool.execute_result('query_official_website',{'name':'测试博物馆'},
            AgentToolContext(self.event.platform,self.event.scope_id,self.event.sender_id,self.event.event_key,event=self.event))
        self.assertEqual(result.payload['official_link']['status'],'verified_navigation')
        self.search.search.assert_not_called()

    def test_search_snippets_do_not_replace_read_checkpoints_or_hide_untitled_root(self):
        self.search.search.return_value=[{'url':'https://encyclopedia.example/item/museum','title':'测试博物馆','snippet':'机构介绍'},
            {'url':'https://museum.example/index.php','title':'Untitled','snippet':'测试博物馆，地址：武汉市测试路'}]
        self.reader.side_effect=PublicHTTPError('forbidden')
        result=self.tool.execute_result('query_official_website',{'name':'测试博物馆'},
            AgentToolContext(self.event.platform,self.event.scope_id,self.event.sender_id,self.event.event_key,event=self.event))
        self.assertEqual(result.payload['official_link']['url'],'https://museum.example/index.php')
        self.assertEqual(self.reader.call_count,1)
        self.assertEqual(self.reader.call_args.args[0],'https://museum.example/index.php')

    def test_public_http_get_preserves_query_and_never_sends_fragment(self):
        response=Mock(status=200,headers={'Content-Type':'text/html'},stream=Mock(return_value=[b'hello']))
        pool=Mock(urlopen=Mock(return_value=response))
        with patch('infrastructure.public_http.resolve_host',return_value=('8.8.8.8',)),patch('infrastructure.public_http.urllib3.HTTPConnectionPool',return_value=pool):
            data,_,_=request_public('http://museum.example/index.php?m=pc#visit',allow_http=True,content_types=('text/html',))
        self.assertEqual(data,b'hello')
        self.assertEqual(pool.urlopen.call_args.args[1],'/index.php?m=pc')
        with self.assertRaises(PublicHTTPError):request_public('http://museum.example',allow_http=True,headers={'Authorization':'secret'})

    def test_phone_and_duration_are_separate_paragraphs_in_fallback(self):
        from tools.research.evidence import render
        result=render({'dates':{},'entries':[{'name':'测试馆','practical':[],
            'extras':[{'topic':'phone','value_raw':'12345'},{'topic':'duration','value_raw':'1小时'}]}]})
        self.assertIn('12345\n\n**建议游玩时长',result)


if __name__=='__main__':unittest.main()
