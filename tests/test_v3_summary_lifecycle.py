from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.conversation_summary_service import select_batch, validate_items


class SummaryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name)/'summary.db')
        self.event = ChatEvent('web', 'group', 'e', 'c', 'u', '继续讨论')
        self.repo = self.store.conversation_summaries
        self.store.save_turn('web:c','u','s1','明确不去夜游'+'甲'*30000,'助手不是事实')

    def test_long_message_keeps_valid_json_and_does_not_advance_unprocessed_turn(self):
        turns = self.repo.pending_turns(self.event)
        payload, _, covered, progress = select_batch(turns, None)
        self.assertLessEqual(len(json.dumps(payload,ensure_ascii=False)),24000)
        self.assertEqual(covered,0)
        self.assertEqual(progress['turn_id'], turns[0]['id'])
        self.assertLess(progress['offset'],len(turns[0]['user_content']))

    def test_each_item_requires_its_own_real_quote(self):
        payload = {'previous_items':[], 'new_turns':[{'id':1,'user':'不去夜游'}]}
        with self.assertRaises(ValueError):
            validate_items({'items':[{'kind':'decision','text':'去夜游',
                'sources':[{'message_id':2,'quote':'不去夜游'}]}]},payload)

    def test_concurrent_generation_claim_has_one_owner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.repo.claim(self.event,None),range(2)))
        self.assertEqual(sum(claim is not None for claim in claims),1)

    def test_lease_covers_gateway_queue_and_call_time(self):
        started=datetime.now(timezone.utc)
        self.repo.claim(self.event,None,started)
        until=datetime.fromisoformat(self.repo.metadata(self.event)['lease_until'])
        self.assertGreater((until-started).total_seconds(),270)

    def test_dependency_change_during_generation_rejects_publication(self):
        turns = self.repo.pending_turns(self.event)
        claim = self.repo.claim(self.event,None)
        self.store.preferences.update(self.event,0,{'pace':'轻松'})
        self.assertIsNone(self.repo.publish(self.event,claim,turns,[],1,{},'fake'))

    def test_deleted_source_rejects_publication(self):
        turns = self.repo.pending_turns(self.event)
        claim = self.repo.claim(self.event,None)
        with self.store._connect() as db:
            db.execute('DELETE FROM conversation_turns')
        self.assertIsNone(self.repo.publish(self.event,claim,turns,[],1,{},'fake'))

    def test_previous_batch_deleted_reference_cannot_be_published(self):
        turns = self.repo.pending_turns(self.event)
        item = {'kind':'decision','text':'不去夜游','sources':[{'message_id':turns[0]['id'],'quote':'不去夜游'}]}
        record = self.repo.publish(self.event,self.repo.claim(self.event,None),turns,[item],turns[0]['id'],{},'fake')
        claim = self.repo.claim(self.event,record)
        with self.store._connect() as db:
            db.execute('DELETE FROM conversation_turns')
        self.assertIsNone(self.repo.publish(self.event,claim,[],json.loads(record['items_json']),1,{},'fake'))

    def test_source_correction_invalidates_even_when_old_quote_still_exists(self):
        turns = self.repo.pending_turns(self.event)
        item = {'kind':'decision','text':'不去夜游','sources':[{'message_id':turns[0]['id'],'quote':'不去夜游'}]}
        self.assertIsNotNone(self.repo.publish(self.event,self.repo.claim(self.event,None),turns,[item],1,{},'fake'))
        with self.store._connect() as db:
            db.execute("UPDATE conversation_turns SET user_content=user_content || '；更正：可以夜游'")
        self.assertIsNone(self.repo.latest(self.event))

    def test_true_quote_does_not_allow_reversed_negation_or_changed_amount(self):
        for text, quote in [('安排夜游','不安排夜游'),('预算900元每天','预算900元全程')]:
            payload = {'previous_items':[], 'new_turns':[{'id':1,'user':quote}]}
            with self.assertRaises(ValueError):
                validate_items({'items':[{'kind':'decision','text':text,
                    'sources':[{'message_id':1,'quote':quote}]}]},payload)

    def test_forgotten_structured_preference_source_is_excluded_on_rebuild(self):
        self.store.preferences.update(self.event,0,{'night':'不安排夜游'})
        with self.store._connect() as db:
            db.execute("UPDATE user_preferences SET source_event='s1'")
        self.store.preferences.update(self.event,1,{'night':None})
        turns = self.repo.pending_turns(self.event)
        self.assertTrue(turns[0]['summary_excluded'])
        payload, _, _, _ = select_batch(turns,None)
        self.assertEqual(payload['new_turns'],[])
        item = {'kind':'decision','text':'不去夜游','sources':[{'message_id':turns[0]['id'],'quote':'不去夜游'}]}
        self.assertIsNone(self.repo.publish(self.event,self.repo.claim(self.event,None),turns,[item],1,{},'fake'))
