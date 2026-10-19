import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.chat_transport import ChatEvent
from core.commands import parse_command
from core.rule_intent import parse_rules,RuleResult,compound_boundary
from core.tool_result import ToolResult
from infrastructure.memory_store import MemoryStore
from services.semantic_task_service import SemanticTaskService


class RuleIntentTests(unittest.TestCase):
    def test_positive_grammar_and_original_evidence(self):
        for city in ('武汉','长沙','北京','西宁','南京','杭州','上海','成都','西安','苏州'):
            for text in (f'查询天气 {city}',f'{city}现在天气怎么样？',f'请查询天气{city}',
                         f'查询路线 {city}站 → {city}博物馆',f'查询天气{city}；查询天气长沙',
                         f'查询天气{city}；查询路线{city}站→{city}博物馆'):
                with self.subTest(text=text):
                    result=parse_rules(text)
                    self.assertEqual(result.status,'complete')
                    self.assertTrue(all(span in text for span in result.ir.source_spans))
        for amount in range(200,220):
            self.assertEqual(parse_rules(f'酒店预算改为{amount}元/间/晚；不要夜游').status,'complete')

    def test_adversarial_tails_never_execute_partial_rule(self):
        tails=('，提醒我买票','；顺便推荐玩法','，不要查','，明天的','，这是什么意思',
               '，如果下雨就取消预约','，再查长沙的','，预算改成300','，安排三天行程',
               '，查询路线武汉站到酒店','，只解释这句话','，不对是长沙')
        for city in ('武汉','长沙','北京','西宁','南京','杭州','上海','成都','西安','苏州'):
            for tail in tails:
                with self.subTest(city=city,tail=tail):
                    self.assertIsNone(parse_rules('查询天气'+city+tail).ir)
                    self.assertEqual(parse_command('查询天气'+city+tail).name,'unknown')

    def test_quoted_negated_and_conditional(self):
        for text in ('不要查询天气武汉','如果下雨就查路线','“查询天气武汉”是什么意思',
                     '查询天气武汉和长沙','查询天气武汉；不要查询天气长沙',
                     '酒店预算改为320元/间/晚；酒店预算改为350元/间/晚'):
            self.assertIsNone(parse_rules(text).ir)
        self.assertFalse(compound_boundary('酒店预算曾经是420；现在是320；医院尚未确定。'))
        self.assertFalse(compound_boundary('请回顾我之前说过什么；有哪些仍未决定？'))
        self.assertTrue(compound_boundary('查询天气武汉；顺便推荐玩法'))
        self.assertTrue(compound_boundary('酒店预算改为320元/间/晚；取消预约提醒'))

    def test_batch_same_domain_zero_model_and_one_root_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp)/'test.db')
            compiler=Mock()
            travel=Mock()
            travel.execute_result.side_effect=lambda tool,args:ToolResult('completed',tool,data=args['location']+'晴')
            service=SemanticTaskService(store,compiler,travel_service=travel)
            event=ChatEvent('web','group','batch','conversation','owner','查询天气武汉；查询天气长沙')
            claim=store.begin_event(event.event_key)
            result=service.handle(event,claim)
            self.assertIn('武汉晴',result); self.assertIn('长沙晴',result)
            compiler.try_compile.assert_not_called()
            self.assertEqual(travel.execute_result.call_count,2)
            with store._connect() as db:
                rows=db.execute('SELECT slots_json FROM travel_tasks').fetchall()
                self.assertEqual(len(rows),1)
                self.assertEqual(len(json.loads(rows[0][0])['operations']),2)
            self.assertEqual(service.handle(event,store.begin_event(event.event_key)),result)
            self.assertEqual(travel.execute_result.call_count,2)

    def test_partial_failure_is_visible_and_not_retried_as_whole_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp)/'test.db')
            compiler=Mock();travel=Mock()
            travel.execute_result.side_effect=[ToolResult('completed','get_current_weather',data='武汉晴'),
                ToolResult('failed','get_current_weather',data='长沙查询失败')]
            service=SemanticTaskService(store,compiler,travel_service=travel)
            event=ChatEvent('web','group','batch','c','o','查询天气武汉；查询天气长沙')
            reply=service.handle(event,store.begin_event(event.event_key))
            self.assertIn('已完成',reply);self.assertIn('失败',reply)
            compiler.try_compile.assert_not_called()

    def test_interruption_resumes_frozen_request_without_reparsing_or_repeating_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp)/'test.db');travel=Mock();compiler=Mock()
            travel.execute_result.side_effect=[ToolResult('completed','get_current_weather',data='武汉晴'),RuntimeError('interruption')]
            service=SemanticTaskService(store,compiler,travel_service=travel)
            event=ChatEvent('web','group','batch','c','o','查询天气武汉；查询天气长沙')
            claim=store.begin_event(event.event_key)
            with self.assertRaises(RuntimeError): service.handle(event,claim)
            reopened=MemoryStore(store.database_path)
            resumed_travel=Mock()
            resumed_travel.execute_result.return_value=ToolResult('completed','get_current_weather',data='长沙晴')
            service=SemanticTaskService(reopened,compiler,travel_service=resumed_travel)
            with patch('services.semantic_task_service.parse_rules',return_value=RuleResult('fallback')):
                reply=service.handle(event,claim)
            self.assertIn('武汉晴',reply);self.assertIn('长沙晴',reply)
            self.assertEqual(resumed_travel.execute_result.call_count,1)
            compiler.try_compile.assert_not_called()
