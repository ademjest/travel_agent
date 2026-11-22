"""Saved six-target replay and constructed semantic/recovery counterexamples."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.chat_transport import ChatEvent
from tools.research.evidence import answer_view
from tools.research.evidence import presentation, render, writing_messages, validate_written, fingerprint, limits, natural_slot, prose_supported
from tools.research.sources import date_check
from core.model_policy import output_limit_for_purpose
from infrastructure.memory_store import MemoryStore
from tools.research.tool import ResearchTool
from tools.research.tool import write_answer
from test_travel_agent import FakeClient, completion, assistant_message

FIXTURE = Path(__file__).parent/'fixtures/wendao_nanjing_six_2026-10-22.json'


def data():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def proposal(base):
    return {'entries':[{k:deepcopy(e[k]) for k in ('target_id','intro','tips','practical')} for e in base['entries']]}


class TravelAnswerTests(unittest.TestCase):
    def test_saved_requests_keep_their_own_prices_and_unquoted_reason(self):
        previous = presentation(json.loads((FIXTURE.parent/'wendao_nanjing_twelve_2026-10-24.json').read_text(encoding='utf-8')))
        latest = presentation(json.loads((FIXTURE.parent/'wendao_nanjing_twelve_2026-10-25.json').read_text(encoding='utf-8')))
        price = lambda entry: next(s for s in entry['practical'] if s['topic']=='ticket_price')
        old, new = price(previous['entries'][0]), price(latest['entries'][0])
        self.assertIn('27元', old['text'])
        self.assertIn('2026-10-25', old['text'])
        self.assertIn('目标日期不同', old['text'])
        self.assertIn('请以官方渠道实时查询为准', new['text'])
        self.assertIn('全日制高校学生凭学生证半价', new['text'])
        self.assertIn('60-69周岁老人半价', new['text'])
        self.assertEqual(new['source_ids'], [1])
        self.assertIn('监护人陪同', new['text'])
        self.assertNotIn('27元', new['text'])
        self.assertEqual(new['answer_state'], 'limited')
        self.assertEqual(len(new['fact_ids']), 2)
        self.assertEqual(price(latest['entries'][10])['text'], '免费')

    def test_latest_museum_keeps_historical_opening_and_all_cultural_sentences(self):
        value = json.loads((FIXTURE.parent/'wendao_nanjing_twelve_2026-10-25.json').read_text(encoding='utf-8'))
        before = deepcopy(value)
        base = presentation(value)
        museum = base['entries'][6]
        self.assertEqual(museum['name'], '南京博物院')
        for detail in ('1933', '2013年扩建后对外开放', '六馆', '鎏金藏式佛塔', '民国时期街道'):
            self.assertIn(detail, museum['intro']['text'])
        self.assertIn('**介绍：**', render(base))
        self.assertIn('鎏金藏式佛塔', render(base))
        self.assertEqual(value, before)

    def test_historical_opening_does_not_admit_current_rules(self):
        historical = '1933年筹建，2013年扩建后对外开放，院内设六馆。'
        rows = [{'id':'m0', 'text':historical}]
        self.assertTrue(prose_supported(historical, rows, ['m0']))
        for text in (historical+'每日09:00开放。', historical+'普通票30元。',
                     historical+'无需预约。', '每日9点对外开放。', '2013年扩建后对外开放至22:00。'):
            with self.subTest(text=text):
                self.assertFalse(prose_supported(text, [{'id':'m0','text':text}], ['m0']))
        self.assertFalse(prose_supported(historical.replace('2013','2027'), rows, ['m0']))

    def test_mixed_intro_retains_culture_with_exact_source_spans(self):
        from tools.research.sources import extract_candidates
        text = '### 测试馆\n介绍：始建于1933年，展陈记录城市历史。每日09:00开放，无需预约。馆内珍藏青瓷和石刻。\n票价：成人票30元。'
        parsed = extract_candidates({'id':1,'text':text,'provider':'ctrip_wendao'}, [{'name':'测试馆','day_index':1}], {1:'2026-10-22'})
        rows = parsed['activity_materials'][0]['intro']
        self.assertEqual(len(rows), 2)
        self.assertIn('石刻', rows[1]['text'])
        for row in rows:
            self.assertEqual(text[row['start']:row['end']], row['text'])
            self.assertTrue(prose_supported(row['text'], [row]))

    def test_tips_are_not_written_rendered_or_reused_as_introductions(self):
        base = presentation(data())
        self.assertTrue(all(not e['tips'] for e in base['entries']))
        messages, supplied = writing_messages(base, '介绍这些景点')
        payload = json.loads(messages[-1]['content'])
        self.assertTrue(all(m['kind']=='intro' for e in payload['entries'] for m in e['materials']))
        tip = next(r for r in base['entries'][0]['materials'] if r['kind']=='tips')
        raw = proposal(base)
        raw['entries'][0]['tips'] = [{'text':tip['text'],'material_ids':[tip['id']]}]
        raw['entries'][0]['intro'] = raw['entries'][0]['tips'][0]
        result = validate_written(raw, base, supplied)
        self.assertEqual(result['entries'][0]['intro'], base['entries'][0]['intro'])
        self.assertFalse(result['entries'][0]['tips'])
        # Compatibility rendering must also suppress tips already saved by old versions.
        result['entries'][0]['tips'] = [{'text':'旧版玩法不应展示'}]
        for detailed in (False, True):
            self.assertNotIn('旧版玩法不应展示', render(result, detailed=detailed))

    def test_research_labels_are_emphasized_once(self):
        base = presentation(data())
        text = render(base, detailed=True)
        for label in ('日期暂定','介绍','票价','开放时间','预约规则','建议游玩时长'):
            self.assertIn('**'+label+'：**', text)
        self.assertNotIn('**介绍：**介绍：', text)

    def test_pending_raw_information_and_missing_information_stay_distinct(self):
        slot = {'answer_state': 'raw_pending', 'topic': 'ticket_price',
                'text': '尚未结构化整理：普通票27元，适用人群未明确。', 'notes': []}
        pending = natural_slot(slot)
        missing = natural_slot({**slot, 'answer_state': 'not_provided', 'text': '来源未提供该项信息。'})
        self.assertIn('27元', pending)
        self.assertNotIn('尚未结构化', pending)
        self.assertNotEqual(pending, missing)

    def test_twelve_target_writing_projects_context_without_losing_conditions(self):
        from infrastructure.model_gateway import estimate_input
        value = json.loads((FIXTURE.parent / 'wendao_nanjing_twelve_2026-10-24.json').read_text(encoding='utf-8'))
        snapshot = {'plan_title': '南京4日行程', 'plan_version': 2,
                    'plan_snapshot': {'content': '完整审计记录，不应重复进入写作输入。' * 3000,
                                      'itinerary_snapshot': {'trip_id': 'TR-test', 'version': 2}}}
        before = deepcopy(snapshot)
        with patch.dict('os.environ', {'AI_BUDGET_TIER': 'economy'}):
            base = presentation(value)
            messages, supplied = writing_messages(base, '分别介绍这些景点', snapshot)
            self.assertLessEqual(estimate_input(messages), limits()[0])
        payload = json.loads(messages[-1]['content'])
        self.assertEqual(payload['snapshot'], {'plan_title': '南京4日行程', 'plan_version': 2, 'trip_id': 'TR-test'})
        self.assertEqual(len(payload['entries']), 12)
        for original, entry in zip(base['entries'], payload['entries']):
            self.assertEqual(entry['target_id'], original['target_id'])
            for full, compact in zip(original['practical'], entry['practical']):
                self.assertEqual(set(compact), {'topic', 'text', 'fact_ids', 'restriction_ids'})
                self.assertEqual(compact['text'], full['text'])
            if original['materials']:
                self.assertTrue(supplied[entry['target_id']])
        self.assertEqual(snapshot, before)

    def test_six_saved_sections_prices_and_culture_are_preserved(self):
        view = answer_view(data())
        self.assertEqual(view['metrics']['collected_target_count'],6)
        for index in (0,2):
            price = next(s for s in view['entries'][index]['slots'] if s['topic']=='ticket_price')
            self.assertIn('27元',price['text'])
            self.assertIn('2026-10-23',price['text'])
            self.assertEqual(price['answer_state'],'limited')
        for index in (1,4):
            price = next(s for s in view['entries'][index]['slots'] if s['topic']=='ticket_price')
            self.assertNotIn('解放门票口',price['text'])
            self.assertNotIn('预约要求',price['text'])
        self.assertTrue(all(e['material']['intro'] for e in view['entries']))
        self.assertIn('1700',''.join(r['text'] for r in view['entries'][0]['material']['intro']))

    def test_date_constraints_intersect(self):
        check=date_check('全年。每年1月1日-12月31日正常开放。','适用日期2026-10-23','2026-10-22')
        self.assertEqual(check['state'],'outside')
        self.assertEqual(check['declared'],'2026-10-23')

    def test_saved_period_conflict_is_disclosed_without_changing_trip(self):
        value=data()
        value['plan_snapshot']={'itinerary_snapshot':{'trip_id':'TR-local','version':1,'days':[{'day_index':1,
            'activities':[{'name':value['target_scope']['targets'][2]['name'],'period':'晚上'}]}]}}
        before=deepcopy(value)
        text=render(presentation(value))
        self.assertIn('当前行程安排在晚上',text)
        self.assertEqual(value,before)

    def test_priority_free_and_paid_extras_are_not_ordinary_admission(self):
        view=answer_view(data())
        temple=next(s for s in view['entries'][3]['slots'] if s['topic']=='ticket_price')
        self.assertIn('未直接提供普通成人票金额',temple['text'])
        self.assertNotEqual(temple['answer_state'],'reference')
        lake=next(s for s in view['entries'][5]['slots'] if s['topic']=='ticket_price')
        self.assertIn('免费',lake['text'])
        self.assertNotIn('78元',lake['text'])
        self.assertEqual(lake['answer_state'],'reference')

    def test_identity_document_is_not_an_admission_audience(self):
        from tools.research.sources import extract_candidates
        source={'id':1,'provider':'ctrip_wendao','type':'provider_answer','text':'### 测试馆\n预约要求：凭电子凭证入园，需携带身份证。'}
        parsed=extract_candidates(source,[{'name':'测试馆','day_index':1}],{1:'2026-10-22'})
        self.assertEqual(parsed['facts'][0]['scope']['visitor_type'],'general')

    def test_readonly_render_has_no_citations_or_duplicate_chapters(self):
        original=data(); before=deepcopy(original)
        base=presentation(original); text=render(base)
        self.assertEqual(original,before)
        self.assertLessEqual(len(text),6000)
        self.assertNotIn('[来源',text)
        self.assertNotIn('尚未结构化',text)
        self.assertIn('日期暂定',text)
        self.assertEqual(text.count('### '),6)
        self.assertIn('云锦',text)

    def test_cultural_paraphrase_accepted_bad_policy_locally_replaced(self):
        base=presentation(data()); messages,supplied=writing_messages(base,'丰富介绍这六处景点')
        raw=proposal(base)
        raw['entries'][0]['intro']['text']='从城墙遗址到青瓷、陶俑，可以慢慢认识六朝时期建康城的生活。'
        bad=raw['entries'][0]['practical'][0]
        bad['text']='全天开放，无需预约。'
        result=validate_written(raw,base,supplied)
        self.assertEqual(result['entries'][0]['intro']['text'],raw['entries'][0]['intro']['text'])
        self.assertEqual(result['entries'][0]['practical'][0]['text'],base['entries'][0]['practical'][0]['text'])
        self.assertTrue(result['degradations'])
        self.assertNotIn('全天开放，无需预约',render(result))

    def test_practical_counterexamples_cannot_hide_in_prose(self):
        base=presentation(data()); _,supplied=writing_messages(base,'介绍')
        for text in ('全部开放到22:00','票价27美元每套','无需预约','必须提前2天购票','官方已核实','2027年始建','《不存在的藏品》值得看'):
            raw=proposal(base); raw['entries'][0]['intro']['text']=text
            with self.subTest(text=text):
                result=validate_written(raw,base,supplied)
                self.assertNotEqual(result['entries'][0]['intro']['text'],text)
        raw=proposal(base); raw['entries'][0]['intro']['material_ids']=['target-2-m0']
        result=validate_written(raw,base,supplied)
        self.assertEqual(result['entries'][0]['intro'],base['entries'][0]['intro'])

    def test_missing_object_falls_back_duplicate_or_unknown_rejects(self):
        base=presentation(data()); _,supplied=writing_messages(base,'介绍')
        raw=proposal(base); raw['entries'].pop()
        self.assertEqual(len(validate_written(raw,base,supplied)['entries']),6)
        for entry in (raw['entries'][0],{'target_id':'other'}):
            bad=deepcopy(raw); bad['entries'].append(entry)
            with self.assertRaises(ValueError): validate_written(bad,base,supplied)

    def test_tiers_and_complete_input_bounds(self):
        from infrastructure.model_gateway import estimate_input
        for tier,output in [('economy',4096),('balanced',6144),('quality',8192)]:
            with patch.dict('os.environ',{'AI_BUDGET_TIER':tier}):
                base=presentation(data()); messages,supplied=writing_messages(base,'介绍')
                self.assertLessEqual(estimate_input(messages),limits()[0])
                self.assertTrue(all(supplied.values()))
                for purpose in ('travel_answer_write','trip_plan','trip_plan_repair'):
                    self.assertEqual(output_limit_for_purpose(purpose,8192),output)

    def test_long_chat_is_bounded_without_truncated_conditions(self):
        base=presentation(data())
        for e in base['entries']:
            for s in e['practical']: s['text']='完整适用条件。'*600
        text=render(base)
        self.assertLessEqual(len(text),6000)
        self.assertIn('请展开完整报告',text)
        self.assertGreater(len(render(base,detailed=True)),8000)


class WritingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=MemoryStore(str(Path(self.tmp.name)/'test.db'))
        self.event=ChatEvent('test','group','m1','scope','owner','介绍这几个景点',occurred_at=datetime.now(timezone.utc))
        self.value=data(); self.store.research.save(self.event,'partial',self.value)

    def tearDown(self): self.tmp.cleanup()

    def test_pre_submission_budget_failure_is_recorded_without_retry(self):
        from infrastructure.execution_report import web_report
        self.store.inbox.submit(self.event.event_key, 'test', 'scope', 'owner', {})
        service = ResearchTool(self.store, None, client=FakeClient([]), model='fake')
        with patch('tools.research.tool.writing_messages', side_effect=ValueError('core_input_budget')):
            write_answer(service, self.event, self.value)
        saved = self.store.research.get(self.event)
        self.assertEqual(saved['answer_write']['status'], 'fallback')
        self.assertFalse(service.client.completions.requests)
        with self.store._connect() as db:
            job_id = db.execute('SELECT id FROM inbox_jobs WHERE event_key=?', (self.event.event_key,)).fetchone()[0]
        report = web_report(self.store.database_path, {'id': job_id, 'event_key': self.event.event_key, 'status': 'completed'})
        self.assertIn('输入超限', report['report_text'])
        self.assertIn('降级', report['state'])
        self.assertTrue(write_answer(service, self.event, saved))
        self.assertFalse(service.client.completions.requests)

    def test_twelve_target_writing_keeps_full_audit_but_sends_compact_context(self):
        value = json.loads((FIXTURE.parent / 'wendao_nanjing_twelve_2026-10-24.json').read_text(encoding='utf-8'))
        value.update(plan_title='南京4日行程', plan_version=2,
            plan_snapshot={'content': '完整行程和审计说明。' * 3000,
                           'itinerary_snapshot': {'trip_id': 'TR-test', 'version': 2}})
        before = deepcopy(value['plan_snapshot'])
        self.store.research.save(self.event, 'partial', value)
        client = FakeClient([completion(assistant_message(json.dumps(proposal(presentation(value)), ensure_ascii=False)))])
        service = ResearchTool(self.store, None, client=client, model='fake')
        with patch.dict('os.environ', {'AI_BUDGET_TIER': 'economy'}):
            write_answer(service, self.event, value)
        saved = self.store.research.get(self.event)
        self.assertEqual(saved['answer_write']['status'], 'completed')
        self.assertEqual(len(client.completions.requests), 1)
        payload = json.loads(client.completions.requests[0]['messages'][-1]['content'])
        self.assertEqual(len(payload['entries']), 12)
        self.assertNotIn('content', payload['snapshot'])
        self.assertEqual(saved['answer_write']['snapshot']['plan_snapshot'], before)
        self.assertEqual(saved['plan_snapshot'], before)

    def test_once_persist_then_replay_and_read_does_not_call(self):
        client=FakeClient([completion(assistant_message(json.dumps(proposal(presentation(self.value)),ensure_ascii=False)))])
        service=ResearchTool(self.store,None,client=client,model='fake')
        first=write_answer(service,self.event,self.value)
        saved=self.store.research.get(self.event)
        self.assertEqual(saved['answer_write']['status'],'completed')
        self.assertEqual(first,write_answer(service,self.event,saved))
        self.assertEqual(first,render(presentation(saved)))
        self.assertEqual(len(client.completions.requests),1)
        self.assertEqual(fingerprint(saved['answer_write']['snapshot']),fingerprint(self.value['answer_write']['snapshot']))

    def test_atomic_reservation_and_unknown_outcome_never_resubmit(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.store.research.reserve_answer_write(self.event,{}),range(2)))
        self.assertEqual(sum(acquired for _,acquired in results),1)
        self.store.research.save(self.event,'reading',data())
        self.assertFalse(self.store.research.reserve_answer_write(self.event,{})[1])
        client=FakeClient([]); service=ResearchTool(self.store,None,client=client,model='fake')
        self.assertTrue(write_answer(service,self.event,self.store.research.get(self.event)))
        self.assertFalse(client.completions.requests)

    def test_timeout_invalid_json_and_length_have_readable_saved_fallback(self):
        for reason in ('invalid','length','timeout'):
            event=ChatEvent('test','group',reason,'scope','owner','介绍',occurred_at=datetime.now(timezone.utc))
            value=data(); self.store.research.save(event,'partial',value)
            response=completion(assistant_message('{'))
            if reason=='length': response.choices[0].finish_reason='length'
            client=FakeClient([response])
            if reason=='timeout':
                def fail(**kwargs): raise TimeoutError('uncertain')
                client.completions.create=fail
            service=ResearchTool(self.store,None,client=client,model='fake')
            text=write_answer(service,event,value)
            self.assertEqual(text.count('### '),6)
            self.assertNotIn('TimeoutError',text)
            self.assertEqual(self.store.research.get(event)['answer_write']['status'],'unknown' if reason=='timeout' else 'fallback')

    def test_cancelled_result_cannot_save_or_publish(self):
        from core.execution_scope import ExecutionRevoked
        client=FakeClient([completion(assistant_message(json.dumps(proposal(presentation(self.value)),ensure_ascii=False)))])
        service=ResearchTool(self.store,None,client=client,model='fake')
        with patch('tools.research.tool.ensure_execution_active', side_effect=[None,ExecutionRevoked('cancelled')]):
            with self.assertRaises(ExecutionRevoked): write_answer(service,self.event,self.value)
        saved=self.store.research.get(self.event)
        self.assertEqual(saved['answer_write']['status'],'reserved')
        self.assertNotIn('result',saved['answer_write'])

    def test_crash_before_save_recovery_does_not_submit_again(self):
        client=FakeClient([completion(assistant_message(json.dumps(proposal(presentation(self.value)),ensure_ascii=False)))])
        service=ResearchTool(self.store,None,client=client,model='fake')
        with patch.object(self.store.research,'save',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): write_answer(service,self.event,self.value)
        self.assertTrue(write_answer(service,self.event,self.store.research.get(self.event)))
        self.assertEqual(len(client.completions.requests),1)

    def test_insufficient_time_and_budget_do_not_retry(self):
        from core.model_budget import ModelBudgetExceeded
        service=ResearchTool(self.store,None,client=FakeClient([]),model='fake')
        with patch('tools.research.tool.remaining_seconds',return_value=12):
            self.assertTrue(write_answer(service,self.event,self.value))
        self.assertFalse(service.client.completions.requests)
        self.assertEqual(self.store.research.get(self.event)['answer_write']['status'],'fallback')
        from dataclasses import replace
        from unittest.mock import Mock
        event=replace(self.event,event_id='quota')
        value=data(); self.store.research.save(event,'partial',value)
        service.client=Mock()
        service.client.chat.completions.create.side_effect=ModelBudgetExceeded('quota')
        self.assertTrue(write_answer(service,event,value))
        self.assertTrue(write_answer(service,event,self.store.research.get(event)))
        self.assertEqual(service.client.chat.completions.create.call_count,1)

    def test_reword_and_evidence_reuse_saved_material_without_provider_calls(self):
        from dataclasses import replace
        from unittest.mock import Mock
        self.value['report']='saved'
        self.store.research.save(self.event,'partial',self.value)
        client=FakeClient([completion(assistant_message(json.dumps(proposal(presentation(self.value)),ensure_ascii=False)))])
        supplier=Mock()
        service=ResearchTool(self.store,None,client=client,model='fake',wendao=supplier)
        rewrite=replace(self.event,event_id='rewrite',content='请把这份资料重新整理，介绍写得更丰富')
        reply=service.handle_intent(rewrite,None)
        self.assertEqual(reply.count('### '),6)
        self.assertEqual(len(client.completions.requests),1)
        self.assertEqual(self.store.research.get(rewrite)['wendao_call']['physical_calls'],0)
        source=replace(self.event,event_id='source',content='查看六朝博物馆的资料原文')
        raw=service.handle_intent(source,None)
        self.assertIn('1700',raw)
        self.assertNotIn('玄武湖景区大门票',raw)
        self.assertEqual(len(client.completions.requests),1)
        supplier.query.assert_not_called()


if __name__ == '__main__': unittest.main()
