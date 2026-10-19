from datetime import date
import unittest
from core.source_applicability import source_applicability


class SourceApplicabilityTests(unittest.TestCase):
    def test_artifact_research_lookup_keeps_scope_and_evidence(self):
        from pathlib import Path
        import tempfile
        from core.chat_transport import ChatEvent
        from infrastructure.memory_store import MemoryStore
        from services.research_service import ResearchService
        import json
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'test.db')
            event=ChatEvent('onebot','group','source','scope','owner','研究')
            item={'text':'来源原文：周一闭馆。','sources':[1],'evidence':[{'source_id':1,'quote':'周一闭馆。'}]}
            data={'report_items':[item],'report_validation':'research-evidence-v1',
                  'sources':[{'id':1,'url':'https://example.test','status':'read','applicability':{'date_status':'unknown'}}]}
            store.research.save(event,'completed',data)
            record=store.research.for_artifact(event,event.event_key)
            payload=json.loads(ResearchService._planning_source(record).split('\n',1)[1])
            self.assertEqual(payload['items'][0]['evidence'],item['evidence'])
            self.assertEqual(payload['sources'][0]['applicability']['date_status'],'unknown')
            from services.trip_service import TripService
            from unittest.mock import Mock
            planner=Mock()
            planner.extract.side_effect=RuntimeError('capture-only')
            artifact={'status':'active','destination':'武汉','kind':'research_backed','source_event_id':event.event_key,
                      'content':'旧报告自由文本','artifact_id':'fixture','version':1}
            with self.assertRaisesRegex(RuntimeError,'capture-only'):
                TripService(store,planner).create_from_artifact(event,None,artifact,1,{})
            actual=planner.extract.call_args.args[3]
            self.assertIn('"evidence"',actual)
            self.assertIn('"applicability"',actual)
            self.assertNotIn('旧报告自由文本',actual)
            other=ChatEvent('onebot','group','adopt','scope','other','采用')
            self.assertIsNone(store.research.for_artifact(other,event.event_key))

    def test_fetch_time_is_never_an_effective_date(self):
        value=source_applicability({'status':'read','text':'周一闭馆。','retrieved_at':'2026-10-18'})
        self.assertEqual(value['date_status'],'unknown')
        self.assertIsNone(value['effective_from'])

    def test_explicit_labelled_ranges_and_city_remain_source_claims(self):
        source={'status':'read','text':'城市：武汉\n生效日期：2024-01-01\n有效期至：2024-12-31'}
        value=source_applicability(source,date(2026,10,18))
        self.assertEqual(value['date_status'],'expired')
        self.assertEqual(value['declared_city'],'武汉')
        self.assertEqual(value['entity_match'],'not_verified')

    def test_snippet_tail_and_invalid_dates_do_not_become_verified(self):
        for source in ({'status':'snippet','text':'生效日期：2024-01-01\n有效期至：2024-12-31'},
                       {'status':'read','text':'x'*4500+'\n有效期至：2024-12-31'},
                       {'status':'read','text':'有效期至：2024-99-99'}):
            self.assertEqual(source_applicability(source)['date_status'],'unknown')
