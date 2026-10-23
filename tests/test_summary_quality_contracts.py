import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.context_builder import ContextBuilder,render_untrusted_context
from core.chat_transport import ChatEvent
from core.context_policy import context_policy
from infrastructure.memory_store import MemoryStore
from scripts.evaluate_summary_quality import ScriptedClient,load_cases,seed_stage,evaluate_case,evaluate_fault,OFFLINE_ENV
from scripts.inspect_conversation_summary import inspect
from services.conversation_summary_service import ConversationSummaryService,select_batch


ROOT=Path(__file__).resolve().parents[1]


class SummaryQualityContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment=patch.dict(os.environ,OFFLINE_ENV)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.corpus=load_cases(ROOT/'evals/summary_quality_cases_v2.json')
        self.store=MemoryStore(Path(self.temp.name)/'test.db')
        self.event=ChatEvent('web','group','current','test','owner','继续普通讨论')
        self.client=ScriptedClient()
        self.events=[]
        self.service=ConversationSummaryService(self.store.conversation_summaries,self.client,'model',self.events.append)
        self.mapping={}

    def seed(self):
        self.client.items=seed_stage(self.store,self.event,self.corpus['cases'][0]['stages'][0],self.corpus,self.mapping)

    def test_disabled_and_not_needed_are_distinct_without_calling_model(self):
        with patch.dict(os.environ,{'AI_SUMMARY_ENABLED':'false'}):
            self.assertIsNone(self.service.ensure(self.event))
        self.assertEqual(self.events[-1]['status'],'disabled')
        self.assertIsNone(self.service.ensure(self.event,needed=False))
        self.assertEqual(self.events[-1]['status'],'not_needed')
        self.assertFalse(self.client.calls)

    def test_turn_and_character_thresholds_are_distinct(self):
        self.service.ensure(self.event)
        self.assertEqual(self.events[-1]['reason'],'turns')
        for i in range(8):
            self.store.save_turn(self.event.storage_scope_id,'owner',f'tiny{i}','普通讨论','收到')
        self.service.ensure(self.event)
        self.assertEqual(self.events[-1]['reason'],'chars')
        self.assertFalse(self.client.calls)

    def test_published_trace_contains_metadata_not_private_content(self):
        self.seed()
        record=self.service.ensure(self.event)
        self.assertIsNotNone(record)
        self.assertEqual(self.events[-1]['status'],'published')
        self.assertEqual(self.events[-1]['version'],1)
        self.assertNotIn('姨妈',json.dumps(self.events,ensure_ascii=False))
        self.assertEqual(self.events[-1]['item_count'],9)

    def test_diagnostic_failure_cannot_break_publication(self):
        self.seed()
        self.service.diagnostics=lambda value: (_ for _ in ()).throw(RuntimeError('diagnostic failure'))
        with self.assertLogs('services.conversation_summary_service',level='WARNING'):
            self.assertIsNotNone(self.service.ensure(self.event))

    def test_model_success_does_not_hide_bad_json_and_cooldown(self):
        self.seed()
        self.client.fault='invalid_json'
        self.assertIsNone(self.service.ensure(self.event))
        self.assertEqual(self.events[-1]['status'],'decode_failed')
        self.assertTrue(self.store.conversation_summaries.metadata(self.event)['retry_after'])
        self.assertIsNone(self.service.ensure(self.event))
        self.assertEqual(self.events[-1]['status'],'cooldown_or_lease_busy')
        self.assertEqual(len(self.client.calls),1)

    def test_truncation_and_bad_reference_are_separately_reported(self):
        for mode,status in [('truncated','output_truncated'),('invalid_reference','validation_failed')]:
            with self.subTest(mode=mode):
                result=evaluate_fault(mode,self.corpus)
                self.assertTrue(result['passed'])
                self.assertEqual(result['diagnostics'][-1]['status'],status)

    def test_previous_version_fallback_is_reported(self):
        self.assertTrue(evaluate_fault('previous_reused',self.corpus)['passed'])

    def test_dependency_source_and_owner_invalidations(self):
        for mode in ('dependency_changed','source_deleted','cross_owner','publish_rejected','network_failure'):
            with self.subTest(mode=mode):
                self.assertTrue(evaluate_fault(mode,self.corpus)['passed'])

    def test_guard_counterexamples_are_fixed_without_changing_gold(self):
        case=next(case for case in self.corpus['cases'] if case['id']=='Q08')
        result=evaluate_case(case,self.corpus)
        self.assertIsNone(result['real_model_quality'])
        self.assertTrue(result['semantic_guard_passed'])
        self.assertEqual([c['id'] for c in result['checks'] if not c['passed']], [])

    def test_summary_payload_never_contains_assistant_guess(self):
        case=next(case for case in self.corpus['cases'] if case['id']=='Q04')
        self.client.items=seed_stage(self.store,self.event,case['stages'][0],self.corpus,self.mapping)
        self.assertIsNotNone(self.service.ensure(self.event))
        payload=self.client.calls[0]['messages'][-1]['content']
        self.assertNotIn('桥边时光',payload)
        self.assertNotIn('轮椅已经借用',payload)

    def test_sources_use_database_ids_not_fixture_positions(self):
        self.store.save_turn('web:other','another','other','不同用户','不属于本次')
        self.seed()
        record=self.service.ensure(self.event)
        self.assertGreater(self.mapping['t1'],1)
        self.assertEqual(json.loads(record['items_json'])[0]['sources'][0]['message_id'],self.mapping['t1'])

    def test_original_facts_can_leak_through_observed_messages(self):
        self.seed()
        for index,user in enumerate(self.corpus['aging_messages'][:7]):
            key=f'partial-{index}'
            self.store.save_turn(self.event.storage_scope_id,'owner',key,user,'中性讨论')
            self.store.save_chat_message(key,'web','test','owner',key,'','user',user)
        context=ContextBuilder(self.store).build(self.event)
        self.assertIn('慢慢看桥',context.group_context)
        self.assertNotIn('慢慢看桥', ''.join(turn.user_content for turn in context.recent_dialogue))

    def test_sixteen_neutral_messages_remove_both_raw_history_channels(self):
        self.seed()
        self.service.ensure(self.event)
        for index,user in enumerate(self.corpus['aging_messages']):
            key=f'aging-{index}'
            self.store.save_turn(self.event.storage_scope_id,'owner',key,user,'中性讨论')
            self.store.save_chat_message(key,'web','test','owner',key,'','user',user)
        traces=[]
        context=ContextBuilder(self.store,self.service,traces.append).build(self.event)
        self.assertIn('慢慢看桥',context.conversation_summary)
        self.assertNotIn('慢慢看桥',context.group_context)
        self.assertNotIn('慢慢看桥',''.join(turn.user_content+turn.assistant_content for turn in context.recent_dialogue))
        self.assertTrue(traces[-1]['summary_injected'])
        self.assertNotIn('慢慢看桥',json.dumps(traces,ensure_ascii=False))

    def test_travel_lookup_does_not_inject_summary(self):
        self.seed()
        self.service.ensure(self.event)
        context=ContextBuilder(self.store,self.service).build(self.event,'travel_lookup')
        self.assertEqual(context.conversation_summary,'')
        self.assertEqual(self.events[-1]['status'],'not_needed')

    def test_batch_boundary_records_unprocessed_next_turn_even_at_zero_offset(self):
        turns=[{'id':i+1,'user_content':'讨论表达方式。'*925,'assistant_content':'简短回复'} for i in range(8)]
        payload,selected,covered,progress=select_batch(turns,None)
        self.assertLess(covered,8)
        self.assertTrue(progress)
        self.assertEqual(progress,{'turn_id':covered+1,'offset':0})
        self.assertLessEqual(len(json.dumps(payload,ensure_ascii=False)),24000)

    def test_summary_budget_never_cuts_inside_item_or_reference(self):
        self.seed()
        record=self.service.ensure(self.event)
        items=[{'kind':'reference','text':'背景'*74,'sources':[{'message_id':100000+i,'quote':'背景'}]} for i in range(12)]
        record={**record,'items_json':json.dumps(items,ensure_ascii=False),
                'summary':'\n'.join(item['text']+' ['+str(item['sources'][0]['message_id'])+']' for item in items)}
        with patch.object(self.service,'ensure',return_value=record):
            for tier in ('economy','balanced','quality'):
                with self.subTest(tier=tier),patch.dict(os.environ,{'AI_BUDGET_TIER':tier}):
                    context=ContextBuilder(self.store,self.service).build(self.event)
                    self.assertTrue(context.conversation_summary.endswith(']'))
                    self.assertLessEqual(len(context.conversation_summary),1800)
                    self.assertLessEqual(context.total_chars,context_policy('tool_reasoning').total_chars)
                    self.assertTrue(set(context.conversation_summary.splitlines())<=set(record['summary'].splitlines()))

    def test_readonly_inspection_has_no_schema_or_data_mutations(self):
        self.seed()
        self.service.ensure(self.event)
        with self.store._connect() as db:
            before=list(db.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
            metadata_before=dict(db.execute('SELECT * FROM conversation_summaries').fetchone())
        result=inspect(self.store.database_path,self.event.storage_scope_id,self.event.sender_id)
        self.assertTrue(result['valid_record'])
        self.assertNotIn('summary',result)
        with self.store._connect() as db:
            self.assertEqual(before,list(db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')))
            self.assertEqual(metadata_before,dict(db.execute('SELECT * FROM conversation_summaries').fetchone()))

    def test_missing_readonly_database_is_not_created(self):
        missing=Path(self.temp.name)/'absent.db'
        with self.assertRaises(FileNotFoundError):
            inspect(missing,'web:test','owner')
        self.assertFalse(missing.exists())

    def test_oversized_item_count_is_rejected_not_silently_saved(self):
        from services.conversation_summary_service import validate_items
        value={'kind':'reference','text':'普通背景','sources':[{'message_id':1,'quote':'普通背景'}]}
        with self.assertRaises(ValueError):
            validate_items({'items':[value for _ in range(13)]},
                           {'previous_items':[],'new_turns':[{'id':1,'user':'普通背景'}]})

    def test_response_to_incremental_correction_replaces_values(self):
        self.seed()
        old=self.service.ensure(self.event)
        self.client.items=seed_stage(self.store,self.event,self.corpus['cases'][1]['stages'][1],self.corpus,self.mapping)
        new=self.service.ensure(self.event)
        self.assertEqual(new['version'],old['version']+1)
        self.assertIn('320',new['summary'])
        self.assertIn('19:00',new['summary'])
        self.assertIn('决定不借用',new['summary'])
        self.assertNotIn('360',new['summary'])
        self.assertNotIn('19:30',new['summary'])
        self.assertNotIn('尚未决定',new['summary'])

    def test_offline_recall_captures_three_conditions_without_raw_leakage(self):
        result=evaluate_case(self.corpus['cases'][0],self.corpus)
        capture=result['context_conditions']
        self.assertTrue(result['mechanism_passed'])
        self.assertFalse(capture['leakage_outside_summary'])
        requests=capture['requests']
        self.assertEqual(set(requests),{'full_original','summary_only','no_summary'})
        for name in ('full_original','summary_only'):
            self.assertIn('慢慢看桥',json.dumps(requests[name]['request'],ensure_ascii=False))
            self.assertIsNone(requests[name]['quality_score'])
        self.assertNotIn('慢慢看桥',json.dumps(requests['no_summary']['request'],ensure_ascii=False))

    def test_manual_templates_are_not_excluded_or_realtime_queries(self):
        from agents.travel_decision import decide_travel_action
        from services.conversation_summary_service import excluded_turn
        messages=json.loads((ROOT/'tests/fixtures/summary_manual_messages.json').read_text(encoding='utf-8'))
        self.assertEqual(len(messages),22)
        for message in messages:
            with self.subTest(message=message[:30]):
                self.assertEqual(decide_travel_action(message).intents,('general',))
                self.assertFalse(excluded_turn(message))


if __name__=='__main__':
    unittest.main()
