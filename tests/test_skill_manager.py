import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from core.model_budget import MODEL_EVENT, model_call_context
from infrastructure.skill_manager import (ROOT, SkillManager, SkillAssetError, SkillVersionUnavailable,
    get_skill_manager, skill_scope, skill_text, STAGE_OWNERS, CONTRACT_FILES)
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from infrastructure.execution_diagnostics import metadata
from test_travel_agent import FakeClient, completion, assistant_message


class SkillManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT/'skills', self.root/'skills')
        target = self.root/'prompts'
        target.mkdir(parents=True)
        shutil.copy(ROOT/'prompts/conversation_summary.md', target)
        for reference in set(STAGE_OWNERS.values()) | {p for paths in CONTRACT_FILES.values() for p in paths}:
            path = self.root/reference
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT/reference, path)
        manifest = json.loads((self.root/'skills/manifest.json').read_text(encoding='utf-8'))
        for entry in manifest['stages']:
            for reference in entry['regression_refs']:
                path = self.root/reference
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

    def change_manifest(self, change):
        path = self.root/'skills/manifest.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        change(value)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_registry_has_explicit_stages_and_no_execution_configuration(self):
        manager = SkillManager(self.root)
        self.assertEqual(len(manager.status()['stages']), 20)
        self.change_manifest(lambda v: v['stages'][0].update(allowed_tools=['confirm_reservation_plan']))
        with self.assertRaises(SkillAssetError):
            SkillManager(self.root)

    def test_duplicate_or_missing_stage_is_rejected(self):
        self.change_manifest(lambda v: v['stages'].append(v['stages'][0]))
        with self.assertRaises(SkillAssetError):
            SkillManager(self.root)

    def test_missing_required_file_is_rejected_at_startup(self):
        (self.root/'skills/trip/plan.json').unlink()
        with self.assertRaises(SkillAssetError):
            SkillManager(self.root)

    def test_missing_repair_block_is_rejected_before_first_call(self):
        path = self.root/'skills/trip/plan.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        del value['feedback']
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        with self.assertRaises(SkillAssetError):
            SkillManager(self.root)

    def test_unsafe_or_outside_asset_path_is_rejected(self):
        for path in ('../outside.md', 'docs/other.md', 'E:/outside.md', 'skills/../../other.md'):
            with self.subTest(path=path):
                self.change_manifest(lambda v: v['stages'][0].update(content={'system':path}))
                with self.assertRaises(SkillAssetError):
                    SkillManager(self.root)

    def test_snapshot_freezes_content_and_resources_but_next_request_refreshes(self):
        manager = SkillManager(self.root)
        path = self.root/'skills/travel-research/sources.json'
        with skill_scope(manager) as first:
            asset = first.assets['research.search_plan']
            before = asset.resource('sources')
            modified = {**before, 'snapshot_test':True}
            path.write_text(json.dumps(modified, ensure_ascii=False), encoding='utf-8')
            with skill_scope(manager) as nested:
                self.assertIs(first, nested)
                self.assertEqual(asset.resource('sources'), before)
            returned = asset.resource('sources')
            returned['aliases'].clear()
            self.assertEqual(asset.resource('sources'), before)
            with self.assertRaises(TypeError):
                first.assets['new'] = asset
        with skill_scope(manager) as second:
            changed = second.assets['research.search_plan']
            self.assertTrue(changed.resource('sources')['snapshot_test'])
            self.assertNotEqual(asset.bundle_hash, changed.bundle_hash)
            resource = 'skills/travel-research/sources.json'
            self.assertEqual(changed.file_hashes[resource], second.assets['research.targets'].file_hashes[resource])

    def test_resources_are_never_implicitly_instructions_and_text_is_not_truncated(self):
        manager = SkillManager(self.root)
        with skill_scope(manager):
            with self.assertRaises(SkillAssetError):
                skill_text('research.search_plan', 'sources')
            policy = skill_text('research.search_plan', 'policy')
            self.assertEqual(policy, (self.root/'skills/travel-research/SKILL.md').read_text(encoding='utf-8'))

    def test_canonical_summary_prompt_and_hash_are_unchanged(self):
        from core.summary_validation import load_summary_prompt
        expected = (ROOT/'prompts/conversation_summary.md').read_text(encoding='utf-8')
        with skill_scope(get_skill_manager()):
            text, sha = load_summary_prompt()
            self.assertEqual(text, expected)
            self.assertEqual(sha, hashlib.sha256(expected.encode()).hexdigest())

    def test_same_purpose_keeps_distinct_stage_assets(self):
        snapshot = SkillManager(self.root).snapshot()
        self.assertEqual(snapshot.assets['materials.image'].purpose, snapshot.assets['reservation.image'].purpose)
        self.assertNotEqual(snapshot.assets['materials.image'].bundle_hash, snapshot.assets['reservation.image'].bundle_hash)

    def gateway(self):
        store = MemoryStore(self.root/'test.db')
        client = FakeClient([completion(assistant_message(content='ok')) for _ in range(3)])
        return store, client, ModelGateway(store, client)

    def test_actual_call_persists_asset_metadata_and_dynamic_system_hash(self):
        store, client, gateway = self.gateway()
        token = MODEL_EVENT.set('test-skill-call')
        self.addCleanup(MODEL_EVENT.reset, token)
        manager = SkillManager(self.root)
        with skill_scope(manager):
            with model_call_context('image_extract', prompt_version='image-context-v1', stage_id='materials.image'):
                gateway.create(model='test', messages=[{'role':'system','content':'one'}])
                gateway.create(model='test', messages=[{'role':'system','content':'two'}])
        with store._connect() as db:
            rows = db.execute('SELECT * FROM model_calls ORDER BY started_at').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['stage_id'], 'materials.image')
        self.assertEqual(rows[0]['asset_version'], 'image-context-v1')
        self.assertEqual(rows[0]['asset_bundle_hash'], rows[1]['asset_bundle_hash'])
        self.assertNotEqual(rows[0]['system_hash'], rows[1]['system_hash'])
        self.assertEqual(len(rows[0]['system_hash']), 64)
        clean = metadata(dict(rows[0]))
        self.assertEqual(clean['asset_bundle_hash'], rows[0]['asset_bundle_hash'])
        self.assertIn('stage_id', clean)
        self.assertNotIn('messages', clean)

    def test_resume_changed_assets_stops_before_call_or_budget_reservation(self):
        store, client, gateway = self.gateway()
        store.delivery.begin_event('test-resume')
        token = MODEL_EVENT.set('test-resume')
        self.addCleanup(MODEL_EVENT.reset, token)
        manager = SkillManager(self.root)
        def submit():
            with skill_scope(manager):
                with model_call_context('image_extract', stage_id='materials.image'):
                    gateway.create(model='test', messages=[{'role':'system','content':'fixed'}])
        submit()
        submit()  # Identical retained bundle is available for continuation.
        path = self.root/'skills/materials/image.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['system'] += '补充'
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        with self.assertRaises(SkillVersionUnavailable):
            submit()
        self.assertEqual(len(client.completions.requests), 2)
        with store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM model_calls').fetchone()[0], 2)

    def test_legacy_unversioned_model_outcome_is_not_resubmitted(self):
        store, client, gateway = self.gateway()
        store.delivery.begin_event('test-legacy')
        token = MODEL_EVENT.set('test-legacy')
        self.addCleanup(MODEL_EVENT.reset, token)
        with model_call_context('image_extract'):
            gateway.create(model='test', messages=[])
        with skill_scope(SkillManager(self.root)):
            with model_call_context('image_extract', stage_id='materials.image'):
                with self.assertRaises(SkillVersionUnavailable):
                    gateway.create(model='test', messages=[])
        self.assertEqual(len(client.completions.requests), 1)

    def test_asset_checkpoint_does_not_change_compact_catalog_authorization(self):
        store, _, gateway = self.gateway()
        store.delivery.begin_event('catalog-test')
        store.delivery.freeze_skill_assets('catalog-test', SkillManager(self.root).snapshot().metadata())
        self.assertEqual(store.delivery.freeze_tool_catalog('catalog-test', 'compact-v1'), 'compact-v1')
