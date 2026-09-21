from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.booking_policy import BookingPolicyResolver, PolicyTextParser, PolicyUnavailable
from services.booking_reminder_service import BookingReminderService


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/hubei_policy_excerpt.json').read_text(encoding='utf-8'))
SIMPLE = '测试博物馆\n个人入馆预约可提前5天。每日0点开始放票。'


class BookingPolicyTests(unittest.TestCase):
    def test_real_excerpt_selects_personal_not_team_or_guide_rule(self):
        resolver = BookingPolicyResolver(fetch_text=lambda url: FIXTURE['text'])
        policy = resolver.resolve('湖北省博物馆')
        self.assertEqual(policy.days_before, 5)
        self.assertEqual(policy.release_time, '00:00')
        self.assertTrue(policy.official)
        self.assertTrue(policy.caveats)
        self.assertNotIn('讲解员', policy.excerpt)
        self.assertEqual(policy.opening_at(date(2026, 10, 1)).isoformat(), '2026-09-26T00:00:00+08:00')

    def test_rules_are_fetched_again_not_hard_coded(self):
        values = iter((SIMPLE, SIMPLE.replace('5天', '6天')))
        resolver = BookingPolicyResolver(sources={'测试博物馆': 'https://museum.example/policy'}, fetch_text=lambda url: next(values))
        first = resolver.resolve('测试博物馆')
        second = resolver.resolve('测试博物馆')
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(second.days_before, 6)

    def test_unknown_boundary_conflicting_rules_and_team_only_do_not_guess(self):
        for text in (
            SIMPLE.replace('5天', '5天（含当天）'),
            '测试博物馆\n团队预约可提前7天。每日0点开始放票。',
            SIMPLE + '\n个人预约可提前7天。每日12点开始放票。',
            SIMPLE.replace('每日0点开始放票。', ''),
            SIMPLE.replace('提前5天', '提前5个工作日'),
        ):
            with self.subTest(text=text), self.assertRaises(PolicyUnavailable):
                BookingPolicyResolver(sources={'测试博物馆': 'https://museum.example/policy'}, fetch_text=lambda url: text).resolve('测试博物馆')

    def test_unregistered_source_is_not_claimed_official(self):
        policy = BookingPolicyResolver(sources={}, fetch_text=lambda url: SIMPLE).resolve('测试博物馆', source_url='https://example.test/policy')
        self.assertFalse(policy.official)
        self.assertTrue(policy.caveats)

    def test_wrong_entity_and_non_https_are_rejected(self):
        resolver = BookingPolicyResolver(fetch_text=lambda url: SIMPLE)
        with self.assertRaises(PolicyUnavailable):
            resolver.resolve('湖北省博物馆')
        with self.assertRaises(PolicyUnavailable):
            resolver.resolve('测试博物馆', source_url='http://example.test/policy')

    def test_parser_excludes_script_and_keeps_split_inline_text(self):
        parser = PolicyTextParser()
        parser.feed('<h1>测试博物馆</h1><script>提前99天</script><p>个人预约可提前<b>5</b>天。每日0点开始放票。</p>')
        self.assertIn('提前5天', parser.text())
        self.assertNotIn('99', parser.text())


class BookingReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'booking.db')
        self.now = datetime(2030, 9, 9, 2, tzinfo=timezone.utc)
        self.text = SIMPLE
        self.resolver = BookingPolicyResolver(sources={'测试博物馆': 'https://museum.example/policy'},
            fetch_text=lambda url: self.text, clock=lambda: self.now)
        self.service = BookingReminderService(self.store, resolver=self.resolver, clock=lambda: self.now)

    def handle(self, text, event_id, user='owner'):
        event = ChatEvent('onebot', 'group', event_id, 'group', user, text)
        claim = self.store.begin_event(event.event_key)
        return self.service.handle(event, claim)

    def reminders(self):
        return self.store.reminders.list_for_owner('onebot', 'group', 'owner')

    def test_clear_rule_creates_derived_reminder_with_traceable_source(self):
        reply = self.handle('我想10月1日去测试博物馆，到了可以预约的日期提醒我预约', 'first')
        self.assertIn('已设置开约提醒', reply)
        rows = self.reminders()
        self.assertEqual(rows[0]['scheduled_at_utc'], '2030-09-25T16:00:00+00:00')
        source = json.loads(rows[0]['source_json'])
        self.assertEqual(source['visit_date'], '2030-10-01')
        self.assertEqual(source['policy']['source_url'], 'https://museum.example/policy')
        self.assertTrue(source['policy']['excerpt'])
        with self.store._connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM booking_policy_evidence').fetchone()[0], 1)

    def test_caveat_requires_owner_confirmation_and_rechecks_source(self):
        self.text += '节假日安排以公告为准。'
        reply = self.handle('10月1日去测试博物馆，开约时提醒我', 'first')
        self.assertIn('核对', reply)
        self.assertEqual(self.reminders(), ())
        self.assertIsNone(self.handle('按这个规则设置提醒', 'other', user='other'))
        self.text = self.text.replace('5天', '6天')
        reply = self.handle('按这个规则设置提醒', 'second')
        self.assertIn('规则内容发生变化', reply)
        self.assertEqual(self.reminders(), ())
        reply = self.handle('按这个规则设置提醒', 'third')
        self.assertIn('已设置开约提醒', reply)
        self.assertEqual(self.reminders()[0]['scheduled_at_utc'], '2030-09-24T16:00:00+00:00')

    def test_unknown_source_preserves_goal_and_accepts_provided_page(self):
        self.resolver.sources = {}
        reply = self.handle('10月1日去测试博物馆，开约时提醒我', 'first')
        self.assertIn('没有创建', reply)
        self.assertEqual(self.reminders(), ())
        reply = self.handle('https://example.test/policy', 'url')
        self.assertIn('尚未独立核实', reply)
        reply = self.handle('按这个规则设置提醒', 'confirm')
        self.assertIn('已设置', reply)
        self.assertFalse(json.loads(self.reminders()[0]['source_json'])['policy']['official'])

    def test_missing_visit_date_can_be_supplied_next_turn(self):
        reply = self.handle('去测试博物馆，开约时提醒我', 'first')
        self.assertIn('哪天', reply)
        reply = self.handle('10月1日', 'date')
        self.assertIn('已设置', reply)

    def test_confirmation_keeps_normalized_date_across_midnight(self):
        self.now = datetime(2030, 9, 9, 15, 55, tzinfo=timezone.utc)
        self.text = SIMPLE.replace('5天', '1天') + '节假日安排以公告为准。'
        self.handle('后天去测试博物馆，开约时提醒我', 'first')
        self.now += timedelta(minutes=10)
        reply = self.handle('按这个规则设置提醒', 'confirm')
        self.assertIn('已经过了', reply)
        self.assertEqual(self.reminders(), ())

    def test_missing_policy_fields_cannot_create_reminder(self):
        self.text = SIMPLE.replace('每日0点开始放票。', '')
        self.assertIn('没有创建', self.handle('10月1日去测试博物馆，开约时提醒我', 'first'))
        self.assertEqual(self.reminders(), ())

    def test_changed_rule_without_previous_caveat_still_requires_new_review(self):
        self.text += '节假日另行通知。'
        self.handle('10月1日去测试博物馆，开约时提醒我', 'first')
        self.text = SIMPLE.replace('5天', '6天')
        reply = self.handle('按这个规则设置提醒', 'confirm')
        self.assertIn('规则内容发生变化', reply)
        self.assertEqual(self.reminders(), ())

    def test_regular_closure_date_is_not_automatically_scheduled(self):
        self.text += '\n每周一闭馆。'
        reply = self.handle('2030-10-07去测试博物馆，开约时提醒我', 'first')
        self.assertIn('闭馆日', reply)
        self.assertEqual(self.reminders(), ())

    def test_querying_policy_never_requires_visit_date_or_creates_reminder(self):
        reply = self.handle('查询测试博物馆预约规则', 'query')
        self.assertIn('提前 5 天', reply)
        self.assertIn('只查询规则', reply)
        self.assertEqual(self.reminders(), ())
        task = self.store.tasks.recent('onebot', 'onebot:group', 'owner')[0]
        self.assertEqual(task.task_type, 'booking_policy_query')
        self.assertEqual(task.status, 'completed')
