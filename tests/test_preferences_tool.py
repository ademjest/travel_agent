from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from core.chat_transport import ChatEvent
from core.intent_ir import parse_intent_ir
from core.preference_contract import preference_fields
from infrastructure.memory_store import MemoryStore
from tools.preferences import PreferencesTool
from app.request_router import RequestRouter


class PreferencesToolTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store=MemoryStore(Path(temp.name)/'prefs.db')
        self.tool=PreferencesTool(self.store.preferences)

    def event(self,text,key='request'):
        return ChatEvent('web','group',key,'conversation','owner',text)

    def proposal(self,text,**patch):
        values={'version':1,'subject':'self','scope':'long_term','polarity':'affirmed',
                'values':{'pace':'轻松'},'evidence':{'pace':text},'authorization':'记住'}
        values.update(patch)
        return parse_intent_ir({'schema_version':1,'action':'update','operations':[
            {'domain':'preferences','operation':'update',**values}], 'source_spans':[text]},text)

    def test_negative_pace_does_not_discard_explicit_positive_preference(self):
        event=self.event('记住，我不喜欢紧凑，但喜欢轻松')
        self.tool.handle_intent(event,self.store.delivery.begin_event(event.event_key),self.proposal(event.content))
        self.assertEqual(self.store.preferences.snapshot(event)['values'],{'pace':'轻松'})
        self.assertEqual(self.tool.extract(event.content),{'pace':'轻松'})

    def test_other_temporary_and_negated_are_not_personal_defaults(self):
        for index,patch in enumerate(({'subject':'other'},{'subject':'unclear'},
                                      {'scope':'current_request'},{'polarity':'negated'})):
            event=self.event('记住，我喜欢轻松',str(index))
            self.tool.handle_intent(event,self.store.delivery.begin_event(event.event_key),self.proposal(event.content,**patch))
            self.assertEqual(self.store.preferences.snapshot(event)['values'],{})

    def test_unknown_person_is_not_accepted_as_self_even_if_model_mislabels(self):
        event=self.event('记住，张教授喜欢轻松')
        self.tool.handle_intent(event,self.store.delivery.begin_event(event.event_key),self.proposal(event.content))
        self.assertEqual(self.store.preferences.snapshot(event)['values'],{})
        self.assertEqual(self.tool.extract(event.content),{})

    def test_fabricated_and_negated_values_fail_grounding(self):
        for text in ('记住，我喜欢紧凑','记住，我不喜欢轻松'):
            with self.assertRaises(ValueError):
                self.proposal(text)

    def test_quoted_instruction_is_not_save_authorization(self):
        event=self.event('举个例子：“记住，我喜欢轻松”')
        self.tool.handle_intent(event,self.store.delivery.begin_event(event.event_key),
                               self.proposal(event.content,evidence={'pace':'我喜欢轻松'}))
        self.assertEqual(self.store.preferences.snapshot(event)['values'],{})

    def test_semantic_request_uses_one_compiler_and_the_same_owner(self):
        event=self.event('我希望你记住，我一般喜欢轻松')
        ir=self.proposal(event.content,evidence={'pace':'我一般喜欢轻松'})
        compiler=Mock(try_compile=Mock(return_value=ir))
        service=RequestRouter(self.store,compiler,preferences_tool=self.tool)
        self.assertIn('已保存偏好',service.handle(event,self.store.delivery.begin_event(event.event_key)))
        compiler.try_compile.assert_called_once()
        self.assertEqual(self.store.preferences.snapshot(event)['values'],{'pace':'轻松'})

    def test_settings_schema_uses_storage_choices_and_forget_marks_old_sources(self):
        event=self.event('记住，以后默认轻松节奏')
        self.tool.handle(event,self.store.delivery.begin_event(event.event_key))
        state=self.tool.snapshot(event)
        self.assertEqual(state['fields'],preference_fields())
        forget=self.event('忘记所有偏好','forget')
        self.tool.handle(forget,self.store.delivery.begin_event(forget.event_key))
        self.assertEqual(self.tool.snapshot(event)['values'],{})
        with self.store._connect() as db:
            sources=[row[0] for row in db.execute('SELECT event_key FROM summary_excluded_events')]
        self.assertIn(event.event_key,sources)
