from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from tools.research.evidence import research_context
from tools.research.evidence import presentation
from tools.research import evidence
from test_wendao_primary_answers import data as saved_data


class EvidenceViewTests(unittest.TestCase):
    def test_chat_report_and_planning_share_one_parse_and_selected_slots(self):
        raw = saved_data()
        original = deepcopy(raw)
        with patch.object(evidence, 'extract_candidates', wraps=evidence.extract_candidates) as parse:
            view = evidence.evidence_view(raw)
            prepared = deepcopy((view.data, view.answer))
            chat = presentation(view)
            evidence.attraction_report(view, detailed=True)
            planning = research_context(view)
            direct = json.loads(evidence.planning_facts(view).split('\n', 1)[1])
            self.assertEqual(parse.call_count, 1)
            self.assertEqual((view.data, view.answer), prepared)
        slots = {(s['target_id'], s['topic']):s for entry in view.answer['entries'] for s in entry['slots']}
        for entry in chat['entries']:
            for row in entry['practical']:
                if row['topic'] == 'schedule_note':
                    continue
                selected = slots[entry['target_id'], row['topic']]
                self.assertEqual(row['fact_ids'], selected['fact_ids'])
                self.assertEqual(row['answer_state'], selected['answer_state'])
        for row in planning['planning_answers']:
            selected = slots[row['target_id'], row['topic']]
            for key in ('fact_ids', 'answer_state', 'verification_state', 'notes'):
                self.assertEqual(row[key], selected[key])
        selected_ids = {key for slot in slots.values() for key in slot['fact_ids']}
        self.assertTrue({evidence.fact_key(f) for f in direct['facts']} <= selected_ids)
        self.assertEqual(raw, original)

    def test_official_claim_with_unsupported_quote_is_not_a_planning_fact(self):
        quote = '全年门票20元。'
        fact = {'schema_version':'attraction-facts-v1','target_id':'target-1','entity_poi_id':None,
                'topic':'ticket_price','value_raw':quote,'source_id':1,'status':'official_verified',
                'scope':{'visit_date':'2030-10-01','visit_mode':'entry','visitor_type':'general','product':'general'},
                'verification':{'identity':'official','date_applicability':'applicable'},
                'evidence':[{'kind':'web_quote','quote':quote,'start':0,'end':len(quote)}]}
        data = {'fact_schema':'attraction-facts-v1','facts':[fact],
                'target_scope':{'targets':[{'name':'测试馆','visit_mode':'entry','day_index':1}],
                                'fields':{'topics':['ticket_price']}},
                'sources':[{'id':1,'type':'web','text':'全年门票80元。','official_identity':{'status':'verified'}}]}
        view = evidence.evidence_view(data)
        self.assertEqual(view.data['facts'][0]['extraction_status'], 'unsupported_evidence')
        self.assertEqual(view.data['facts'][0]['verification']['content_support'], 'unsupported_quote')
        self.assertFalse(view.planning_candidates)
        self.assertEqual(data['facts'][0]['status'], 'official_verified')

    def test_prepared_view_is_isolated_from_later_input_mutations(self):
        data = saved_data()
        view = evidence.evidence_view(data)
        before = presentation(view)
        data['sources'].clear()
        data['facts'].clear()
        self.assertEqual(presentation(view), before)
