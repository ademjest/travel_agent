import json
from pathlib import Path
import unittest
import os
import tempfile
from unittest.mock import Mock, patch

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from scripts.evaluate_summary_quality import OFFLINE_ENV, ScriptedClient, load_cases, seed_stage
from services.conversation_summary_service import ConversationSummaryService

from services.conversation_summary_service import select_batch, validate_items


class SummaryV4SemanticsTests(unittest.TestCase):
    def validate(self, text, quote, kind='reference'):
        return validate_items({'items': [{'kind': kind, 'text': text, 'sources': [
            {'message_id': 7, 'quote': quote}]}]},
            {'previous_items': [], 'new_turns': [{'message_id': 7, 'user': quote}]})

    def test_correct_current_time_with_obsolete_negation(self):
        self.validate('最晚结束时间由20:30改为19:30',
                      '最晚结束时间改为19:30。此前20:30不再适用。', 'correction')

    def test_equivalent_currency_and_chinese_number(self):
        self.validate('每间每晚360 CNY', '每间每晚360元人民币')
        self.validate('姨妈2个月前做过手术', '姨妈两个月前做过手术')

    def test_live_equivalent_negation_and_rate_wording(self):
        self.validate('同行者是姨妈而非父母', '同行者是姨妈，不是父母。')
        self.validate('同行者是姨妈，并非父母', '同行者是姨妈，不是父母。')
        self.validate('费用上限是每间每晚360元人民币，也不是按每人每天计价。',
                      '费用上限是每间每晚360元人民币，不是每人每天360元。')
        for text in ('同行者是父母而非姨妈', '同行者是父母，并非姨妈'):
            with self.assertRaises(ValueError):
                self.validate(text, '同行者是姨妈，不是父母。')
        with self.assertRaises(ValueError):
            self.validate('不是按每人每天400元计价', '不是每人每天420元，另有400元的一次费用')

    def test_equivalent_negation_cannot_be_cut_from_original_quote(self):
        for marker in ('而非', '并非'):
            with self.assertRaises(ValueError):
                validate_items({'items': [{'kind': 'reference', 'text': '同行者是父母', 'sources': [
                    {'message_id': 7, 'quote': '父母'}]}]},
                    {'previous_items': [], 'new_turns': [{'message_id': 7, 'user': '同行者是姨妈' + marker + '父母'}]})

    def test_task_requirement_prefix_is_not_mistaken_for_a_person(self):
        self.validate('本次活动要求连续行走最多15分钟，随后坐下休息；这是活动限制，不是医疗咨询。',
                      '本次活动限制是：连续行走最多15分钟，随后坐下休息；不安排需要连续上下楼梯的活动。'
                      '这是本次活动要求，不是医疗咨询。')

    def test_assistant_proposal_ownership_correction_is_equivalent(self):
        self.validate('先前提到的名称和决定并非用户本人的决定；处理相关信息时应遵循用户原话，不自行补全。',
                      '你刚才提到的名称和决定都不是我的决定。请按我的原话处理，不自行补全。', 'correction')
        self.validate('不要将未经用户明确表达的名称或决定归于用户；应按用户原话处理，不自行补全。',
                      '你刚才提到的名称和决定都不是我的决定。请按我的原话处理，不自行补全。', 'correction')
        self.validate('不能把未经用户确认的名字或选择当作用户的决定。',
                      '助手提出的名字和选择不是我的决定。', 'correction')
        self.validate('名称和决定不是用户的决定。',
                      '你刚才提到的名称和决定都不是我的决定。', 'correction')

    def test_ownership_correction_cannot_bypass_other_facts(self):
        source = '你刚才提到的名称和决定都不是我的决定。请按我的原话处理，不自行补全。'
        for text in ('不要将未经用户表达的医院归于用户。',
                     '这些名称和决定是用户本人的决定。',
                     '这些名称和决定不是用户本人的决定；预算500元。',
                     '这些名称和决定都是用户的决定。',
                     '不要将未经用户明确表达的名称或决定归于用户；预算500元。',
                     '不要将未经用户明确表达的名称或决定归于用户；已购买门票。',
                     '不要将未经用户明确表达的名称或决定归于用户；不是姨妈，是父母。'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.validate(text, source)
        with self.assertRaises(ValueError):
            self.validate('不要将未经用户明确表达的名称或决定归于用户。', '我已确认助手提出的名称和决定。')

    def test_reversed_negation_and_units(self):
        for text, quote in [
            ('不是姨妈，是父母同行', '不是父母，是姨妈同行'),
            ('不是每间每晚420元，而是每人每天420元', '每间每晚420元，不是每人每天420元'),
            ('安排夜游', '不安排夜游'),
            ('预算900元每天', '预算900元全程'),
            ('连续行走至少15分钟', '连续行走最多15分钟'),
            ('决定借用轮椅', '轮椅是否借用尚未决定'),
            ('不需要轮椅', '决定不借用轮椅'),
            ('无条件参加活动', '如果天气晴朗才参加活动'),
            ('已购买门票', '希望购买门票'),
            ('姨妈连续行走最多40分钟，阿诚连续行走最多15分钟',
             '姨妈连续行走最多15分钟，阿诚连续行走最多40分钟'),
            ('住宿每间每晚80元，餐饮每人每天360元', '住宿每间每晚360元，餐饮每人每天80元'),
            ('住宿预算每人每天360元', '住宿预算每间每晚360元，餐饮预算每人每天360元'),
            ('小林预算每人每天360元', '小林预算每间每晚360元，小周预算每人每天360元'),
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.validate(text, quote)

    def test_does_not_invent_currency(self):
        with self.assertRaises(ValueError):
            self.validate('预算360 CNY', '预算360元')

    def test_current_negative_decision_can_replace_prior_uncertainty(self):
        source = '轮椅是否借用尚未决定，要等姨妈体验后再讨论。轮椅事项现在决定不借用。'
        self.validate('轮椅事项现已决定不借用，不再是等待姨妈体验后讨论的未决事项。', source, 'correction')
        for text in ('轮椅事项已决定借用。', '轮椅事项已经借用。',
                     '轮椅事项已决定不购买。', '其他事项已决定不借用。'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.validate(text, source)
        for tentative in ('轮椅事项可能现在决定不借用。', '轮椅事项希望现在决定不借用。'):
            with self.subTest(source=tentative), self.assertRaises(ValueError):
                self.validate('轮椅事项现已决定不借用。', '轮椅是否借用尚未决定。' + tentative)

    def test_quote_cannot_strip_immediately_preceding_negation(self):
        with self.assertRaises(ValueError):
            validate_items({'items': [{'kind': 'decision', 'text': '安排夜游', 'sources': [
                {'message_id': 7, 'quote': '安排夜游'}]}]},
                {'previous_items': [], 'new_turns': [{'message_id': 7, 'user': '不安排夜游'}]})

    def test_schema_error_has_safe_path_and_limit(self):
        from core.summary_contract import SummaryValidationError
        with self.assertRaises(SummaryValidationError) as caught:
            validate_items({'items': [{'kind': 'reference', 'text': '背景',
                'sources': [{'message_id': 7, 'quote': '背景'}] * 5}]},
                {'new_turns': [{'message_id': 7, 'user': '背景'}], 'previous_items': []})
        self.assertEqual(caught.exception.details, {'code': 'sources_count', 'path': 'items[0].sources',
                                                   'actual': 5, 'max': 4})

    def test_batch_uses_message_id_and_preserves_input_order(self):
        payload, _, _, _ = select_batch([
            {'id': 9, 'user_content': '较早的消息'}, {'id': 2, 'user_content': '较新的消息'}], None)
        self.assertEqual([turn['message_id'] for turn in payload['new_turns']], [9, 2])
        self.assertFalse(any('id' in turn for turn in payload['new_turns']))

    def test_prompt_is_exact_user_file_and_versioned_by_content(self):
        from core.summary_contract import load_summary_prompt, PROMPT_VERSION
        prompt, digest = load_summary_prompt()
        path = Path(__file__).resolve().parents[1] / 'prompts/conversation_summary.md'
        import hashlib
        self.assertEqual(prompt, path.read_text(encoding='utf-8'))
        self.assertEqual(digest, hashlib.sha256(prompt.encode()).hexdigest())
        self.assertTrue(PROMPT_VERSION.startswith('conversation-summary-v4'))

    def test_shared_schema_matches_wire_contract(self):
        from core.summary_contract import OUTPUT_SCHEMA, MAX_ITEMS, MAX_SOURCES, MAX_QUOTE_CHARS
        items = OUTPUT_SCHEMA['properties']['items']
        sources = items['items']['properties']['sources']
        self.assertEqual(items['maxItems'], MAX_ITEMS)
        self.assertEqual(sources['maxItems'], MAX_SOURCES)
        self.assertEqual(sources['items']['properties']['quote']['maxLength'], MAX_QUOTE_CHARS)

    def test_legacy_model_output_is_not_current_wire_contract(self):
        with self.assertRaises(ValueError):
            validate_items({'items': [], 'summary': 'old', 'open_questions': []},
                           {'previous_items': [], 'new_turns': []})

    def test_previous_source_hash_is_not_model_input(self):
        previous = {'items_json': json.dumps([{'kind': 'reference', 'text': '旧材料', 'sources': [
            {'message_id': 7, 'quote': '旧材料', 'source_hash': 'server-only'}]}]),
            'progress_json': '{}', 'covered_turn_id': 7}
        payload, _, _, _ = select_batch([], previous)
        self.assertNotIn('source_hash', payload['previous_items'][0]['sources'][0])

    def test_captured_responses_keep_source_limit_but_accept_correct_time(self):
        from scripts.replay_summary_responses import replay
        root = Path(__file__).resolve().parents[1]
        result = replay(root / 'tests/fixtures/summary_captured_responses_v3.json')
        self.assertEqual(result['real_model_calls'], 0)
        calls = {call['physical_call']: call for call in result['results']}
        self.assertEqual([failure['item'] for failure in calls[1]['failures']], [11])
        self.assertEqual([failure['item'] for failure in calls[7]['failures']], [9])
        self.assertNotIn(6, [failure['item'] for failure in calls[8]['failures']])


class SummaryV4ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, OFFLINE_ENV)
        environment.start()
        self.addCleanup(environment.stop)
        self.store = MemoryStore(Path(self.temp.name) / 'isolated.db')
        self.event = ChatEvent('web', 'group', 'v4', 'isolated', 'owner', '继续讨论')
        corpus = load_cases(Path(__file__).resolve().parents[1] / 'evals/summary_quality_cases_v2.json')
        self.client = ScriptedClient()
        self.client.items = seed_stage(self.store, self.event, corpus['cases'][0]['stages'][0], corpus, {})
        self.diagnostics = []
        self.service = ConversationSummaryService(self.store.conversation_summaries, self.client,
                                                  'offline', self.diagnostics.append)

    def test_publication_stores_reviewed_prompt_hash_and_version(self):
        from core.summary_contract import PROMPT_VERSION, load_summary_prompt
        record = self.service.ensure(self.event)
        self.assertEqual(record['prompt_version'], PROMPT_VERSION)
        self.assertEqual(record['prompt_hash'], load_summary_prompt()[1])
        self.assertEqual(self.client.calls[0]['messages'][0]['content'], load_summary_prompt()[0])
        self.assertNotIn('saved_business_states', json.loads(self.client.calls[0]['messages'][-1]['content']))

    def test_captured_q04_publishes_every_item_without_repair_or_discard(self):
        root = Path(__file__).resolve().parents[1]
        audit = json.loads((root / 'tests/fixtures/summary_captured_q04_v4.json').read_text(encoding='utf-8'))
        captured = next(call for call in audit['physical_requests'] if call['event_key'] == 'summary-live:Q04')
        payload = json.loads(captured['request']['messages'][-1]['content'])
        store = MemoryStore(Path(self.temp.name) / 'q04.db')
        event = ChatEvent('web', 'group', 'captured-q04', 'q04', 'owner', '继续讨论')
        mapping = {}
        for turn in payload['new_turns']:
            key = f'q04-{turn["message_id"]}'
            store.save_turn(event.storage_scope_id, event.sender_id, key, turn['user'], 'Neutral text. ' * 120)
            with store._connect() as db:
                mapping[turn['message_id']] = db.execute('SELECT id FROM conversation_turns WHERE user_msg_id=?', (key,)).fetchone()[0]
        sdk = ScriptedClient()
        sdk.items = [{**item, 'sources': [{'message_id': mapping[ref['message_id']], 'quote': ref['quote']}
                                         for ref in item['sources']]}
                     for item in json.loads(captured['content'])['items']]
        record = ConversationSummaryService(store.conversation_summaries, sdk, 'offline').ensure(event)
        self.assertIsNotNone(record)
        self.assertEqual(len(json.loads(record['items_json'])), 3)
        self.assertIn('尚未确定', record['summary'])
        self.assertIn('尚未决定', record['summary'])
        self.assertEqual(len(sdk.calls), 1)

    def test_no_implicit_repair_without_budget_permit(self):
        self.client.fault = 'invalid_json'
        self.assertIsNone(self.service.ensure(self.event))
        self.assertEqual(len(self.client.calls), 1)

    def test_explicit_repair_is_once_and_revalidated(self):
        self.service.repair_permit = Mock(return_value=True)
        create = self.client.create

        def first_bad(**kwargs):
            self.client.fault = 'invalid_json'
            response = create(**kwargs)
            if len(self.client.calls) == 2:
                response.choices[0].message.content = json.dumps({'items': self.client.items}, ensure_ascii=False)
            return response

        self.client.chat.completions.create = first_bad
        self.assertIsNotNone(self.service.ensure(self.event))
        self.assertEqual(len(self.client.calls), 2)
        self.service.repair_permit.assert_called_once()
        self.assertEqual(self.diagnostics[-1]['repair_attempt'], 1)

    def test_failed_repair_does_not_loop(self):
        self.service.repair_permit = Mock(return_value=True)
        self.client.fault = 'invalid_json'
        self.assertIsNone(self.service.ensure(self.event))
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.diagnostics[-1]['repair_attempt'], 1)

    def test_budget_refusal_and_bad_source_never_retry(self):
        self.service.repair_permit = Mock(return_value=False)
        self.client.fault = 'invalid_json'
        self.assertIsNone(self.service.ensure(self.event))
        self.assertEqual(len(self.client.calls), 1)
        self.service.repair_permit.assert_called_once()

    def test_invalid_source_cannot_be_repaired_into_authorization(self):
        self.service.repair_permit = Mock(return_value=True)
        self.client.fault = 'invalid_reference'
        self.assertIsNone(self.service.ensure(self.event))
        self.assertEqual(len(self.client.calls), 1)
        self.service.repair_permit.assert_not_called()

    def test_contract_drift_fails_closed_without_claim_or_model(self):
        with patch('services.conversation_summary_service.load_summary_prompt', side_effect=ValueError('mismatch')):
            self.assertIsNone(self.service.ensure(self.event))
        self.assertFalse(self.client.calls)
        self.assertIsNone(self.store.conversation_summaries.metadata(self.event))
        self.assertEqual(self.diagnostics[-1]['status'], 'prompt_failed')


if __name__ == '__main__':
    unittest.main()
