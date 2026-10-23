from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.attraction_facts import bind_poi, page_facts, provider_facts, reconcile_facts, validate_api_fact
from core.chat_transport import ChatEvent
from core.execution_scope import ExecutionRevoked
from core.research_evidence import official_page_evidence, validate_research_items
from core.source_applicability import rule_applicability
from infrastructure.amap_client import normalize_poi_detail
from infrastructure.memory_store import MemoryStore
from infrastructure.public_http import request_public, PublicHTTPError
from infrastructure.web_reader import PageLinks
from services.official_sources import OfficialSources, scoped_url
from services.research_service import ResearchService


def entry(name='测试博物馆', **changes):
    return {'entity_key': 'test-museum', 'canonical_name': name, 'aliases': [], 'city': '武汉',
            'allowed_hosts': ['museum.example'], 'path_prefixes': ['/visitor'],
            'policy_urls': ['https://museum.example/visitor/policy'], 'notice_index_urls': [],
            'identity_evidence_url': 'https://museum.example/visitor/policy',
            'identity_evidence_quote': name + '版权所有', 'operator_name': name,
            'verified_at': '2026-10-19', 'registry_version': 'test-1', **changes}


def page(text, url='https://museum.example/visitor/policy'):
    return {'url': url, 'title': '测试博物馆', 'text': text, 'status': 'read', 'content_hash': 'test-hash',
            'retrieved_at': '2026-10-19T09:00:00+00:00', 'links': []}


class OfficialContractTests(unittest.TestCase):
    def setUp(self):
        self.entry = entry()
        self.registry = OfficialSources([self.entry])

    def verified(self, text):
        return {**page(text), 'id': 1, 'official_identity': self.registry.verify('测试博物馆', 'https://museum.example/visitor/policy')}

    def test_registry_is_entity_city_host_and_path_scoped(self):
        self.assertIsNone(self.registry.match('测试博物馆', '北京'))
        for url in ('https://museum.example.evil.test/visitor/policy', 'https://other.museum.example/visitor/policy',
                    'https://museum.example/visitor-other', 'https://museum.example/visitor/%2e%2e/elsewhere',
                    'https://museum.example/visitor/policy?key=secret', 'http://museum.example/visitor/policy'):
            self.assertFalse(scoped_url(url, self.entry), url)
        self.assertTrue(scoped_url('https://museum.example/visitor/notice.html', self.entry))

    def test_shared_host_does_not_trust_another_account(self):
        record = entry(allowed_hosts=['shared.example'], path_prefixes=['/museum-a'],
                       policy_urls=['https://shared.example/museum-a/policy'])
        self.assertFalse(scoped_url('https://shared.example/museum-b/policy', record))

    def test_different_city_and_multiple_same_name_never_choose_first(self):
        raw = {'id': 'B00001', 'name': '测试博物馆', 'typecode': '140100', 'cityname': '武汉市'}
        one = normalize_poi_detail(raw, '/v5/place/text', '2026-10-19T09:00:00+00:00')
        two = {**one, 'poi_id': 'B00002'}
        self.assertIsNone(bind_poi({'name': '测试博物馆'}, '武汉', [one, two]))
        self.assertIsNone(bind_poi({'name': '测试博物馆'}, '北京', [one]))
        self.assertIsNone(bind_poi({'name': '测试博物馆'}, '武汉', [{**one, 'name': '测试博物馆南门'}]))
        self.assertIsNone(bind_poi({'name': '测试博物馆'}, '武汉', [{**one, 'adcode': '420105'}],
                                   entry(adcode='420106')))

    def test_today_is_fetch_day_in_beijing_not_message_or_future_day(self):
        detail = normalize_poi_detail({'id': 'B00001', 'business': {'opentime_today': '09:00-17:00', 'cost': '50'}},
                                      '/v5/place/detail', '2026-10-18T16:01:00+00:00')
        facts = provider_facts(detail, 1, 'target-1', '2026-10-19', 'entry')
        self.assertEqual(facts[0]['verification']['date_applicability'], 'same_query_day')
        validate_api_fact(facts[0], detail)
        self.assertEqual(provider_facts(detail, 1, 'target-1', '2026-11-03', 'entry')[0]['verification']['date_applicability'], 'not_target_day')
        facts[0]['value_raw'] = '免费'
        with self.assertRaises(ValueError):
            validate_api_fact(facts[0], detail)

    def test_official_entity_title_and_complete_rule_are_separate_quotes(self):
        source = self.verified('测试博物馆\n生效日期：2026-01-01\n有效期至：2026-12-31\n本馆免费开放。')
        target = {'name': '测试博物馆', 'visit_mode': 'entry'}
        facts = page_facts(source, target, 'target-1', ['ticket_price'], '2026-11-03', self.entry)
        self.assertEqual(facts[0]['status'], 'official_verified')
        self.assertEqual({ref['kind'] for ref in facts[0]['evidence']}, {'web_entity', 'web_quote'})
        for ref in facts[0]['evidence']:
            self.assertEqual(source['text'][ref['start']:ref['end']], ref['quote'])
        with self.assertRaises(ValueError):
            official_page_evidence(source, '免费', '测试博物馆')

    def test_unverified_page_keeps_strict_entity_quote_and_never_official(self):
        source = {**page('测试博物馆全年免费开放。'), 'id': 1, 'official_identity': {'status': 'unverified'}}
        facts = page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 'target-1', ['ticket_price'], '2026-11-03')
        self.assertEqual(facts[0]['status'], 'unverified_source')
        source['text'] = '本馆全年免费开放。'
        self.assertEqual(page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 'target-1', ['ticket_price'], '2026-11-03'), [])

    def test_truncated_page_tail_cannot_be_complete_rule(self):
        source = self.verified('测试博物馆\n本馆免费开放但仅限')
        source['truncated'] = True
        with self.assertRaises(ValueError):
            official_page_evidence(source, '本馆免费开放但仅限', '测试博物馆')

    def test_subhall_closure_does_not_close_whole_museum(self):
        source = self.verified('测试博物馆\n我馆将于2026年11月3日对明清馆临时闭馆。')
        self.assertEqual(page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 'target-1', ['opening_hours'], '2026-11-03', self.entry), [])

    def test_headline_and_effective_date_only_do_not_fill_topic_coverage(self):
        source = self.verified('测试博物馆\n雕版馆临时闭馆公告\n五、本预约须知自2025年8月1日起实施。')
        self.assertEqual(page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 'target-1',
                                  ['opening_hours', 'reservation_rules'], '2026-11-03', self.entry), [])

    def test_dates_are_independent_of_retrieval_and_month_without_year_is_unknown(self):
        source = page('测试博物馆')
        self.assertEqual(rule_applicability(source, '即日起闭馆。', date(2026, 11, 3))['date_status'], 'unknown')
        self.assertEqual(rule_applicability(source, '11月3日临时闭馆。', date(2026, 11, 3))['date_status'], 'unknown')
        self.assertEqual(rule_applicability(source, '每周一闭馆，国家法定节假日除外。', date(2026, 11, 3))['date_status'], 'unknown')
        self.assertEqual(rule_applicability(source, '2026年10月19日（周一）至10月23日（周五）临时闭馆。', date(2026, 10, 21))['date_status'], 'applicable')
        self.assertEqual(rule_applicability(source, '2026年12月30日至次年1月3日临时闭馆。', date(2027, 1, 2))['date_status'], 'applicable')
        self.assertEqual(rule_applicability(source, '2026年12月30日至1月3日临时闭馆。', date(2027, 1, 2))['date_status'], 'unknown')

    def test_since_now_requires_an_actual_publication_field_not_crawl_time(self):
        source = page('测试博物馆')
        source['publication_evidence'] = {'kind': 'html_meta', 'field': 'article:published_time', 'raw_value': '2026-10-21T10:00:00+08:00'}
        self.assertEqual(rule_applicability(source, '本馆即日起暂停开放。', date(2026, 10, 20))['date_status'], 'not_yet_effective')
        self.assertEqual(rule_applicability(source, '本馆即日起暂停开放。', date(2026, 10, 22))['effective_from'], '2026-10-21')
        source['publication_evidence']['raw_value'] = '2026-10-21T16:30:00Z'
        self.assertEqual(rule_applicability(source, '本馆即日起暂停开放。', date(2026, 10, 21))['date_status'], 'not_yet_effective')
        self.assertEqual(rule_applicability(source, '本馆即日起暂停开放。', date(2026, 10, 22))['effective_from'], '2026-10-22')
        source['publication_evidence']['raw_value'] = '2026-10-21T16:30:00'
        self.assertEqual(rule_applicability(source, '本馆即日起暂停开放。', date(2026, 10, 22))['date_status'], 'unknown')

    def test_same_scope_official_conflicts_do_not_pick_newest(self):
        target = {'name': '测试博物馆', 'visit_mode': 'entry'}
        a = page_facts(self.verified('测试博物馆\n全年开放时间09:00-17:00。'), target, 'target-1', ['opening_hours'], '2026-11-03', self.entry)
        b = page_facts({**self.verified('测试博物馆\n全年开放时间10:00-18:00。'), 'id': 2}, target, 'target-1', ['opening_hours'], '2026-11-03', self.entry)
        self.assertEqual({row['status'] for row in reconcile_facts(a + b)}, {'conflict'})

    def test_third_duplicate_source_does_not_win_by_majority(self):
        target = {'name': '测试博物馆', 'visit_mode': 'entry'}
        facts = []
        for index, text in enumerate(('全年门票价格60元。', '全年门票价格80元。', '全年门票价格60元。')):
            facts.extend(page_facts({**self.verified('测试博物馆\n' + text), 'id': index + 1}, target,
                'target-1', ['ticket_price'], '2026-11-03', self.entry))
        self.assertEqual({row['status'] for row in reconcile_facts(facts)}, {'conflict'})

    def test_explicit_closure_overrides_regular_hours_not_reverse(self):
        target = {'name': '测试博物馆', 'visit_mode': 'entry'}
        regular = page_facts(self.verified('测试博物馆\n全年开放时间09:00-17:00。'), target, 'target-1', ['opening_hours'], '2026-11-03', self.entry)
        closure = page_facts({**self.verified('测试博物馆\n2026年11月3日本馆临时闭馆。'), 'id': 2}, target, 'target-1', ['opening_hours'], '2026-11-03', self.entry)
        reconcile_facts(regular + closure)
        self.assertEqual(regular[0]['status'], 'unknown')
        self.assertEqual(closure[0]['status'], 'official_verified')

    def test_interior_rule_cannot_verify_an_exterior_visit(self):
        source = self.verified('测试博物馆\n本馆全年免费开放。')
        facts = page_facts(source, {'name': '测试博物馆', 'visit_mode': 'exterior'}, 'target-1', ['ticket_price'], '2026-11-03', self.entry)
        self.assertEqual(facts[0]['status'], 'unknown')
        self.assertEqual(facts[0]['scope']['visit_mode'], 'entry_not_applicable_to_exterior')

    def test_interactive_hall_rule_is_not_general_admission(self):
        source = self.verified('测试博物馆\n本馆全年免费开放。\n为了提供良好的互动体验，馆内数字馆仍需提前预约。')
        facts = page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 'target-1',
                           ['ticket_price', 'reservation_rules'], '2026-11-03', self.entry)
        rule = next(f for f in facts if f['topic'] == 'reservation_rules')
        self.assertEqual(rule['scope']['product'], 'special')
        self.assertNotEqual(rule['status'], 'official_verified')

    def test_public_redirect_policy_rejects_before_requesting_foreign_host(self):
        response = Mock(status=302, headers={'Location': 'https://evil.example/visitor/policy'})
        pool = Mock()
        pool.urlopen.return_value = response
        with patch('infrastructure.public_http.resolve_host', return_value=('8.8.8.8',)), \
             patch('infrastructure.public_http.urllib3.HTTPSConnectionPool', return_value=pool):
            with self.assertRaises(PublicHTTPError):
                request_public('https://museum.example/visitor/policy', redirects=2,
                               url_allowed=lambda url: scoped_url(url, self.entry))
        self.assertEqual(pool.urlopen.call_count, 1)

    def test_link_extraction_handles_html_base_without_inventing_doubled_path(self):
        links = PageLinks('https://museum.example/mu/frontend/pg/index')
        links.feed('<base href="/mu/"><a href="frontend/pg/visit">参观须知</a><a href="/bad?key=secret">预约</a>')
        self.assertEqual(links.links, [{'url': 'https://museum.example/mu/frontend/pg/visit', 'text': '参观须知'}])


class AttractionPipelineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MemoryStore(Path(folder.name) / 'test.db')
        self.event = ChatEvent('onebot', 'group', 'facts', 'scope', 'owner', '核对方案景点的开放、门票和预约',
                               occurred_at=datetime(2026, 10, 19, tzinfo=timezone.utc))
        self.request = {'plan': {'title': '武汉方案', 'destination': '武汉', 'version': 1, 'duration_options': [1]},
                        'original_request': self.event.content, 'visit_start_date_text': '2026年11月3日'}
        self.scope = {'targets': [{'name': '测试博物馆', 'day_index': 1, 'optional': False, 'visit_mode': 'entry'}],
            'fields': {'topics': ['opening_hours', 'ticket_price', 'reservation_rules'], 'visit_start_date_text': '2026年11月3日'},
            'requested_domains': [], 'excluded_domains': [], 'strict_sources': False, 'language': 'zh-cn'}
        self.search = SimpleNamespace(configured=False, search=Mock(side_effect=AssertionError('No search key')))
        self.registry = OfficialSources([entry()])
        self.reader = Mock(side_effect=lambda url, **kwargs: page(
            '测试博物馆\n生效日期：2026-01-01\n有效期至：2026-12-31\n本馆免费开放。\n本馆每日开放时间09:00-17:00。\n本馆实行实名预约参观制度。', url))
        detail = normalize_poi_detail({'id': 'B00001', 'name': '测试博物馆', 'cityname': '武汉市', 'typecode': '140100',
            'business': {'opentime_week': '周二至周日09:00-17:00', 'cost': '50'}}, '/v5/place/text', '2026-10-19T09:00:00+00:00')
        self.amap = SimpleNamespace(search_poi_details=Mock(return_value=[detail]), get_poi_detail=Mock(return_value=detail))
        self.service = ResearchService(self.store, self.search, reader=self.reader, amap=self.amap, official_sources=self.registry)
        self.service.search_planner.plan_targets = Mock(return_value=self.scope)

    def run_facts(self):
        return self.service.handle_intent(self.event, None, request=self.request)

    def test_without_tavily_runs_amap_and_registered_site_without_model(self):
        reply = self.run_facts()
        self.assertIn('官方依据已核验', reply)
        self.assertIn('不是门票价格', reply)
        self.search.search.assert_not_called()
        self.assertEqual(self.amap.search_poi_details.call_count, 1)
        self.assertEqual(self.reader.call_count, 1)
        data = self.store.research.get(self.event)
        self.assertEqual(data['coverage']['covered_count'], 3)
        self.assertEqual(data['fact_schema'], 'attraction-facts-v1')
        self.assertEqual(len({source['id'] for source in data['sources']}), len(data['sources']))
        self.assertTrue(all(fact['source_id'] in {row['id'] for row in data['sources']} for fact in data['facts']))
        with self.store._connect() as db:
            for table in ('personal_reminders', 'policy_watches', 'reservation_plans', 'trips'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_amap_failure_does_not_disable_official_pages(self):
        self.amap.search_poi_details.side_effect = RuntimeError('No v5 permission')
        self.assertIn('官方依据已核验', self.run_facts())
        self.assertEqual(self.reader.call_count, 1)

    def test_future_today_field_is_platform_reference_not_official(self):
        self.run_facts()
        data = self.store.research.get(self.event)
        self.assertTrue(all(f['status'] == 'provider_reference' for f in data['facts'] if f['evidence'][0]['kind'] == 'api_field'))

    def test_strict_site_does_not_sneak_in_amap_or_registered_other_site(self):
        self.scope.update(strict_sources=True, requested_domains=['only.example'])
        reply = self.run_facts()
        self.amap.search_poi_details.assert_not_called()
        self.reader.assert_not_called()
        self.assertIn('未知', reply)

    def test_no_internet_cancels_all_sources(self):
        from dataclasses import replace
        self.event = replace(self.event, content='不要联网核对这些景点')
        self.assertIn('没有发起', self.run_facts())
        self.amap.search_poi_details.assert_not_called()
        self.reader.assert_not_called()

    def test_unknown_user_url_and_redirect_never_gain_official_identity(self):
        self.reader.side_effect = lambda url, **kwargs: page('测试博物馆全年免费开放。', 'https://evil.example/visitor/policy')
        self.run_facts()
        data = self.store.research.get(self.event)
        self.assertFalse(any(f['status'] == 'official_verified' for f in data['facts']))
        self.assertEqual(next(s for s in data['sources'] if s['type'] == 'web')['status'], 'unavailable')

    def test_completed_checkpoints_do_not_repeat_external_calls(self):
        self.run_facts()
        self.run_facts()
        self.assertEqual(self.amap.search_poi_details.call_count, 1)
        self.assertEqual(self.reader.call_count, 1)

    def test_fragment_aliases_share_one_page_and_fact_extraction(self):
        self.registry.entries[0]['policy_urls'].append('https://museum.example/visitor/policy#booking')
        self.run_facts()
        self.assertEqual(self.reader.call_count, 1)
        data = self.store.research.get(self.event)
        self.assertEqual(data['request_counts']['pages'], 1)
        self.assertEqual(len([s for s in data['sources'] if s['type'] == 'web']), 1)

    def test_different_urls_identical_body_keep_alias_but_do_not_duplicate_facts(self):
        self.registry.entries[0]['policy_urls'].append('https://museum.example/visitor/copy')
        self.run_facts()
        data = self.store.research.get(self.event)
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(len([s for s in data['sources'] if s['status'] == 'duplicate']), 1)
        self.assertEqual(len([f for f in data['facts'] if f['status'] == 'official_verified']), 3)

    def test_full_report_is_separate_from_bounded_reply_and_reusable_without_calls(self):
        reply = self.run_facts()
        data = self.store.research.get(self.event)
        self.assertLessEqual(len(reply), 6000)
        self.assertEqual(reply, data['brief_report'])
        self.assertIn('来源与核验依据', data['report'])
        self.assertNotEqual(reply, data['report'])
        self.assertEqual(self.run_facts(), reply)
        self.assertEqual(self.reader.call_count, 1)

    def test_only_amap_does_not_call_search_or_pages(self):
        from dataclasses import replace
        self.event = replace(self.event, content='只看高德补充景点信息')
        self.search.configured = True
        self.run_facts()
        self.reader.assert_not_called()
        self.search.search.assert_not_called()
        self.assertEqual(self.amap.search_poi_details.call_count, 1)

    def test_signed_user_link_is_not_requested_or_saved_as_a_source_url(self):
        from dataclasses import replace
        self.event = replace(self.event, content='只看 https://only.example/policy?access_token=private')
        self.run_facts()
        self.reader.assert_not_called()
        self.assertFalse(any('access_token' in source.get('url', '') for source in self.store.research.get(self.event)['sources']))

    def test_legacy_amap_double_without_v5_does_not_break_official_query(self):
        self.service.attraction_research.amap = SimpleNamespace(search_places=Mock())
        self.assertIn('官方依据已核验', self.run_facts())

    def test_team_policy_does_not_fill_general_visit_coverage(self):
        self.reader.side_effect = lambda url, **kwargs: page('测试博物馆\n团队每日需提前预约。', url)
        self.run_facts()
        self.assertEqual(self.store.research.get(self.event)['coverage']['covered_count'], 0)

    def test_resume_after_second_page_cancellation_keeps_first_read(self):
        registry = OfficialSources([entry(policy_urls=['https://museum.example/visitor/policy', 'https://museum.example/visitor/notice'])])
        self.service.attraction_research.registry = registry
        seen = []
        def reader(url, **kwargs):
            seen.append(url)
            if url.endswith('/notice') and seen.count(url) == 1:
                raise ExecutionRevoked('interrupted')
            return page('测试博物馆\n本馆全年免费开放。', url)
        self.reader.side_effect = reader
        with self.assertRaises(ExecutionRevoked):
            self.run_facts()
        self.run_facts()
        self.assertEqual(seen.count('https://museum.example/visitor/policy'), 1)
        self.assertEqual(self.amap.search_poi_details.call_count, 1)

    def test_planning_keeps_structured_conditions_and_source_ids(self):
        self.run_facts()
        data = self.store.research.get(self.event)
        text = self.service._planning_source(data)
        value = json.loads(text.split('\n', 1)[1])
        self.assertEqual(value['schema_version'], 'attraction-facts-v1')
        self.assertTrue(value['facts'])
        self.assertTrue(all('scope' in f and 'verification' in f and 'evidence' in f for f in value['facts']))
        self.assertLessEqual(len(text), 6000)
        self.assertTrue(all(f['source_id'] in {s['id'] for s in value['sources']} for f in value['facts']))

    def test_revocation_is_not_swallowed_as_amap_failure(self):
        self.amap.search_poi_details.side_effect = ExecutionRevoked('cancelled')
        with self.assertRaises(ExecutionRevoked):
            self.run_facts()
        self.reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
