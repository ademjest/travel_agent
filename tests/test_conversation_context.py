from app.request_router import RequestRouter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.conversation_context import ContextBuilder, select_batch
from core.chat_transport import ChatEvent
from core.summary_validation import excluded_turn
from core.tasks import TaskUpdate
from infrastructure.memory_store import MemoryStore


class ConversationContextContracts(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store=MemoryStore(Path(temp.name)/'context.db')
        self.repo=self.store.conversation_summaries
        self.event=ChatEvent('web','group','request','conversation','owner','回顾刚才讨论的事项')

    def save(self,text,key):
        event=ChatEvent('web','group',key,'conversation','owner',text)
        self.store.delivery.begin_event(event.event_key)
        self.store.conversations.save_turn(event.storage_scope_id,event.sender_id,event.event_key,text,'收到')
        return event

    def test_source_category_preserves_ordinary_permission_discussion(self):
        text='博物馆拍摄权限需要到现场确认。'
        self.save(text,'permission')
        turns=self.repo.pending_turns(self.event)
        self.assertEqual(turns[0]['source_kind'],'conversation')
        self.assertTrue(excluded_turn(text))  # Untagged historical rows retain their old guard.
        payload,_,_,_=select_batch(turns,None)
        self.assertEqual(payload['new_turns'][0]['user'],text)

    def test_explicit_exclusion_wins_without_sensitive_keywords(self):
        event=ChatEvent('web','group','preference','conversation','owner','轻松')
        claim=self.store.delivery.begin_event(event.event_key)
        self.store.preferences.update(event,0,{'pace':'轻松'},claim=claim,reply='已保存')
        self.store.conversations.save_turn(event.storage_scope_id,event.sender_id,event.event_key,event.content,'已保存')
        turns=self.repo.pending_turns(self.event)
        self.assertEqual(turns[0]['source_kind'],'excluded')
        self.assertEqual(select_batch(turns,None)[0]['new_turns'],[])

    def test_context_and_summary_share_one_source_snapshot(self):
        self.save('讨论明天的安排','source')
        summary=Mock(ensure=Mock(return_value={'covered_turn_id':0,'items_json':'[]','summary':''}))
        with patch.dict('os.environ',{'AI_SUMMARY_ENABLED':'true'}), patch.object(
                self.repo,'pending_turns',wraps=self.repo.pending_turns) as pending:
            context=ContextBuilder(self.store,summary).build(self.event)
        pending.assert_called_once()
        summary.ensure.assert_called_once()
        self.assertIn('讨论明天的安排',context.summary_pending_context)
        self.assertTrue(summary.ensure.call_args.kwargs['sources'][1][0]['summary_eligible'])

    def test_source_limit_cannot_claim_complete_after_advancing_watermark(self):
        for index in range(102):
            self.store.conversations.save_turn('web:conversation','owner',str(index),'讨论','收到')
        summary=Mock(ensure=Mock(return_value={'covered_turn_id':100,'items_json':'[]','summary':''}))
        with patch.dict('os.environ',{'AI_SUMMARY_ENABLED':'true'}):
            context=ContextBuilder(self.store,summary).build(self.event)
        self.assertTrue(context.summary_information_incomplete)
        self.assertEqual(context.summary_pending_context,'')

    def test_cancelled_task_fences_inflight_summary_source(self):
        event=ChatEvent('web','group','source','conversation','owner','明天提醒我出发')
        claim=self.store.delivery.begin_event(event.event_key)
        self.store.tasks.prepare_result(event,claim,TaskUpdate('reminder','collecting',event.content,{},('time',)),'几点？')
        self.store.conversations.save_turn('web:conversation','owner',event.event_key,event.content,'几点？')
        turns=self.repo.pending_turns(self.event)
        summary_claim=self.repo.claim(self.event,None)
        cancel=ChatEvent('web','group','cancel','conversation','owner','取消当前任务')
        RequestRouter(self.store, None).route(cancel,self.store.delivery.begin_event(cancel.event_key))
        self.assertIsNone(self.repo.publish(self.event,summary_claim,turns,[],turns[0]['id'],{},'offline'))
