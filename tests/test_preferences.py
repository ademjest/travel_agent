from pathlib import Path
import tempfile
import unittest

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from infrastructure.preference_repository import PreferenceConflict
from services.preference_service import PreferenceService
from services.trip_service import TripService


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name)/'test.db')
        self.repo = self.store.preferences
        self.service = PreferenceService(self.repo)

    def event(self, text='', scope='a', owner='local-owner', platform='web', key='e'):
        return ChatEvent(platform, 'group', key, scope, owner, text)

    def handle(self, text, key):
        event = self.event(text, key=key)
        return self.service.handle(event, self.store.begin_event(event.event_key))

    def test_web_same_owner_cross_conversation_and_qq_isolated(self):
        self.handle('记住，以后优先公共交通，轻松节奏', 'save')
        self.assertEqual(self.repo.snapshot(self.event(scope='b'))['values']['transport'], '公共交通')
        self.assertEqual(self.repo.snapshot(self.event(owner='other'))['values'], {})
        self.assertEqual(self.repo.snapshot(self.event(platform='onebot'))['values'], {})
        e = self.event(platform='onebot')
        self.repo.update(e, 0, {'pace': '紧凑'})
        self.assertEqual(self.repo.snapshot(self.event(platform='onebot', scope='other-group'))['values'], {})
        fresh = MemoryStore(self.store.database_path)
        self.assertEqual(fresh.preferences.snapshot(self.event())['values']['pace'], '轻松')

    def test_candidate_requires_acceptance_and_forget_invalidates_late_write(self):
        self.assertIn('目前未保存', self.handle('我一般喜欢博物馆', 'suggest'))
        self.assertEqual(self.repo.snapshot(self.event())['values'], {})
        candidate = self.repo.candidate(self.event())
        self.repo.update(self.event(), 0, {}, clear=True)
        self.assertIsNone(self.repo.candidate(self.event()))
        with self.assertRaises(PreferenceConflict): self.repo.update(self.event(), candidate['version'], {'pace': '轻松'})
        self.assertFalse(self.repo.suggest(self.event(), 0, {'pace': '轻松'}))

    def test_explicit_save_once_and_current_trip_not_personal_profile(self):
        event = self.event('记住，以后优先公共交通')
        claim = self.store.begin_event(event.event_key)
        self.service.handle(event, claim)
        self.service.handle(event, claim)
        self.assertEqual(self.repo.snapshot(event)['version'], 1)
        self.assertEqual(self.service.extract('记住这次带父母，轻松一点'), {})
        self.assertEqual(self.service.extract('记住，小王喜欢紧凑'), {})

    def test_structured_trip_default_respects_explicit_request(self):
        self.repo.update(self.event(), 0, {'pace': '轻松', 'transport': '公共交通', 'night': '不安排夜游'})
        service = TripService(self.store, None)
        spec = {'preferences': ['紧凑节奏'], 'transport_text': '驾车', 'gentle': False}
        service._apply_preferences(self.event('这次紧凑一点，驾车'), spec)
        self.assertFalse(spec['gentle'])
        self.assertEqual(spec['transport_text'], '驾车')
        self.assertTrue(spec['no_night'])
        self.assertEqual(self.repo.snapshot(self.event())['values']['pace'], '轻松')
        other = {'preferences': [], 'transport_text': ''}
        service._apply_preferences(self.event('帮我规划武汉三天行程'), other)
        self.assertTrue(other['gentle'])
        self.assertEqual(other['transport_text'], '公共交通')

    def test_disable_and_budget_units_and_unknown_fields(self):
        for value in ({'owner': 'other'}, {'budget': {'amount': 500}}, {'pace': 'fake'}):
            with self.assertRaises(ValueError): self.repo.update(self.event(), 0, value)
        self.repo.update(self.event(), 0, {'pace': '轻松'}, enabled=False)
        self.assertEqual(self.repo.defaults(self.event())['values'], {})
        self.assertEqual(self.repo.snapshot(self.event())['values']['pace'], '轻松')

    def test_negation_and_research_request_cannot_accidentally_save_preferences(self):
        self.assertIn('没有保存', self.handle('不要记住，我喜欢公共交通', 'negative'))
        self.assertEqual(self.repo.snapshot(self.event())['values'], {})
        self.assertIsNone(self.handle('按我的偏好搜索武汉攻略并规划三天行程', 'research'))
