"""Saved Xi'an response and heading/cache boundary counterexamples; no real calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from tools.research.sources import extract_candidates, answer_targets
from tools.research.evidence import answer_view, presentation, render
from tools.research.tool import ResearchTool, write_answer

FIXTURE=Path(__file__).parent/'fixtures/wendao_xian_six_2026-10-27.json'


def saved():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class SavedSectionTests(unittest.TestCase):
    def test_actual_response_preserves_all_objects_facts_and_introductions(self):
        value=saved();source=value['sources'][0]
        self.assertEqual(hashlib.sha256(source['text'].encode()).hexdigest(),value['text_sha256'])
        view=answer_view(value)
        self.assertEqual(view['metrics']['collected_target_count'],6)
        self.assertEqual(view['metrics']['states']['not_provided'],0)
        self.assertEqual(view['metrics']['official_verified_count'],0)
        base=presentation({k:v for k,v in value.items() if k!='answer_write'})
        self.assertEqual(sum(bool(e['intro']) for e in base['entries']),6)
        self.assertIn('54元',base['entries'][1]['practical'][0]['text'])
        self.assertIn('120元',base['entries'][2]['practical'][0]['text'])
        for entry in view['entries']:
            part=entry['section']['sections'][0]
            self.assertGreater(len(source['text'][part['start']:part['end']].strip()),80)
        for entry in base['entries']:
            for row in entry['materials']:
                self.assertEqual(source['text'][row['start']:row['end']].replace('<','').replace('>',''),row['text'])

    def test_old_empty_written_cache_cannot_hide_reprocessed_material(self):
        value=saved();before=deepcopy(value)
        result=presentation(value)
        self.assertEqual(sum(s['answer_state']=='not_provided' for e in result['entries'] for s in e['practical']),0)
        self.assertEqual(sum(bool(e['intro']) for e in result['entries']),6)
        self.assertTrue(result['cache_reprocessed'])
        self.assertEqual(value,before)

    def test_current_written_cache_is_preserved(self):
        value=saved();value.pop('answer_write')
        base=presentation(value);base['generation']='llm'
        value['answer_write']={'result':base}
        self.assertEqual(presentation(value)['generation'],'llm')

    def test_changed_visit_scope_invalidates_written_material(self):
        value=saved();value.pop('answer_write')
        value['answer_write']={'result':presentation(value)}
        value['dates'].update(visit_start_date='2026-11-03',visit_end_date='2026-11-05')
        result=presentation(value)
        self.assertTrue(result['cache_reprocessed'])
        self.assertEqual(result['entries'][0]['visit_date'],'2026-11-03')

    def test_completed_event_replay_projects_fresh_material_without_resubmitting_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp)/'test.db');value=saved()
            event=ChatEvent('onebot','group','saved','scope','owner','介绍这些景点')
            store.research.save(event,'partial',value)
            client=Mock();tool=ResearchTool(store,Mock(configured=False),client=client)
            answer=write_answer(tool,event,value)
            self.assertIn('54元',answer)
            client.chat.completions.create.assert_not_called()
            self.assertEqual(store.research.get(event)['answer_write'],value['answer_write'])

    def test_report_read_repairs_projection_without_network_or_database_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp)/'test.db')
            event=ChatEvent('onebot','group','saved','scope','owner','这些景点的信息再为我介绍一下')
            value=saved();value['report']='old empty report';store.research.save(event,'partial',value)
            before=store.research.get(event)
            search=Mock(configured=False);reader=Mock();client=Mock()
            tool=ResearchTool(store,search,client=client,reader=reader)
            reports=tool.web_reports(event)
            self.assertIn('54元',reports[0]['report'])
            self.assertIn('**介绍：**',reports[0]['report'])
            self.assertTrue(reports[0]['presentation']['replayed_saved_answer'])
            self.assertEqual(store.research.get(event),before)
            search.search.assert_not_called();reader.assert_not_called();client.chat.completions.create.assert_not_called()


class HeadingBoundaryTests(unittest.TestCase):
    def parse(self,text,targets=None):
        return extract_candidates({'id':1,'text':text,'provider':'ctrip_wendao'},targets or [{'name':'测试馆','day_index':1}],{1:'2026-10-28'})

    def test_nested_markdown_and_bold_material_are_inside_the_object(self):
        text='### 测试馆\n**介绍**\n馆藏青瓷和石刻记录城市的文化记忆。\n#### 展陈特色\n青瓷与建筑相互映照，适合慢慢参观。\n**开放时间**\n09:00-17:00\n**普通票价**\n成人票30元\n'
        result=self.parse(text)
        self.assertTrue(any(f['topic']=='opening_hours' for f in result['facts']))
        self.assertTrue(any(f['topic']=='ticket_price' for f in result['facts']))
        self.assertTrue(result['activity_materials'][0]['intro'])

    def test_unrequested_peer_object_cannot_leak_into_target(self):
        text='### 测试馆\n**介绍**\n这里的历史展陈适合慢慢参观。\n开放时间：09:00-17:00\n### 另一座馆\n普通票价：成人票99元\n'
        self.assertFalse(any('99元' in f['value_raw'] for f in self.parse(text)['facts']))

    def test_bold_object_headings_keep_peer_isolation(self):
        text='**测试馆**\n**介绍**\n这里的历史展陈适合慢慢参观。\n开放时间：09:00-17:00\n**另一座馆**\n票价：成人票99元\n'
        result=self.parse(text)
        self.assertTrue(result['activity_materials'][0]['intro'])
        self.assertFalse(any('99元' in f['value_raw'] for f in result['facts']))

    def test_general_discovery_does_not_promote_intro_or_container_to_object(self):
        text='# 参考攻略\n**介绍**\n这是一份城市旅游参考。\n## 第一天\n### 测试馆\n**介绍**\n这里的历史展陈适合慢慢参观。\n开放时间：09:00-17:00\n### 测试公园\n**介绍**\n沿湖林荫适合散步。\n票价：免费\n'
        self.assertEqual([t['name'] for t in answer_targets(text)],['测试馆','测试公园'])

    def test_empty_returned_section_is_processing_pending_not_source_missing(self):
        value={'sources':[{'id':1,'text':'### 测试馆\n','provider':'ctrip_wendao'}],
               'provider_targets':[{'name':'测试馆','day_index':1}]}
        view=answer_view(value)
        self.assertEqual(view['metrics']['states']['not_provided'],0)
        self.assertEqual(view['metrics']['invalid_section_count'],1)
        self.assertEqual(view['answer_stage'],'partial')
        self.assertNotIn('现有资料未提供这项信息',render(presentation(value)))

    def test_all_empty_sections_do_not_consume_a_writing_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MemoryStore(Path(tmp)/'test.db')
            event=ChatEvent('onebot','group','empty','scope','owner','介绍测试馆')
            value={'sources':[{'id':1,'text':'### 测试馆\n','provider':'ctrip_wendao'}],
                   'provider_targets':[{'name':'测试馆','day_index':1}]}
            store.research.save(event,'partial',value)
            client=Mock();tool=ResearchTool(store,Mock(configured=False),client=client)
            answer=write_answer(tool,event,value)
            self.assertIn('正文为空',answer)
            client.chat.completions.create.assert_not_called()
            self.assertNotIn('answer_write',store.research.get(event))
