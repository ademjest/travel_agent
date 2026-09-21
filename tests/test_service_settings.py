import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from adapters.web_app import create_web_app
from core.settings import Settings
from core.web_settings import WebSettings
from infrastructure.env_settings_store import EnvSettingsStore, SettingsConflict


class ServiceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.path = self.root / '.env'
        self.path.write_text("# existing\nLLM_API_KEY='old-model-fixture'\nLLM_BASE_URL='https://model.example/v1'\nAMAP_API_KEY=old-map-fixture\nCUSTOM_OPTION=keep\n", encoding='utf-8')
        self.store = EnvSettingsStore(self.root, inherited={})

    def save(self, changes):
        return self.store.save(self.store.status()['version'], changes)

    def test_search_only_save_preserves_existing_keys_comments_and_empty_fields(self):
        result = self.save({'SEARCH_API_KEY': "fixture-key-'\\#", 'LLM_API_KEY': '', 'AMAP_API_KEY': ''})
        value = dotenv_values(self.path)
        self.assertEqual(value['SEARCH_API_KEY'], "fixture-key-'\\#")
        self.assertEqual(value['LLM_API_KEY'], 'old-model-fixture')
        self.assertEqual(value['AMAP_API_KEY'], 'old-map-fixture')
        self.assertEqual(value['CUSTOM_OPTION'], 'keep')
        self.assertIn('# existing', self.path.read_text())
        self.assertTrue(result['restart_required'])
        serialized = json.dumps(result)
        for secret in ('old-model-fixture', 'old-map-fixture', 'fixture-key'):
            self.assertNotIn(secret, serialized)
        if os.name != 'nt': self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_null_clears_and_suppresses_parent_fallback(self):
        (self.root.parent / '.env').write_text('SEARCH_API_KEY=parent-fixture')
        self.save({'SEARCH_API_KEY': None})
        self.assertEqual(dotenv_values(self.path)['SEARCH_API_KEY'], '')
        self.assertFalse(self.store.status()['fields']['SEARCH_API_KEY']['configured'])

    def test_source_override_and_version_conflicts(self):
        controlled = EnvSettingsStore(self.root, inherited={'LLM_API_KEY': 'env-fixture'})
        self.assertFalse(controlled.status()['fields']['LLM_API_KEY']['editable'])
        with self.assertRaises(ValueError): controlled.save(controlled.status()['version'], {'LLM_API_KEY': 'new'})
        version = self.store.status()['version']
        self.path.write_text(self.path.read_text() + 'ADDED=other\n')
        with self.assertRaises(SettingsConflict): self.store.save(version, {'SEARCH_API_KEY': 'fixture'})

    def test_injection_unknown_fields_and_endpoint_rebind_rejected(self):
        before = self.path.read_bytes()
        for changes in ({'SEARCH_API_KEY': 'a\nLLM_API_KEY=overwrite'}, {'SEARCH_API_KEY': '${SECRET}'},
                        {'PATH': 'arbitrary'}, {'LLM_BASE_URL': 'https://evil.example?key=fixture'}):
            with self.assertRaises(ValueError): self.save(changes)
        with patch('infrastructure.env_settings_store.public_address'):
            with self.assertRaises(ValueError): self.save({'LLM_BASE_URL': 'https://other.example/v1'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_replace_keeps_original_and_removes_own_temp(self):
        before = self.path.read_bytes()
        with patch('infrastructure.env_settings_store.os.replace', side_effect=OSError('injected')):
            with self.assertRaises(OSError): self.save({'SEARCH_API_KEY': 'fixture'})
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(list(self.root.glob('.env.settings-*')), [])

    def test_git_tracked_or_unignored_env_is_blocked(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, check=True)
        git('init')
        with self.assertRaises(ValueError): self.save({'SEARCH_API_KEY': 'fixture'})
        (self.root / '.gitignore').write_text('.env\n.env.*\n!.env.example\n')
        self.save({'SEARCH_API_KEY': 'fixture'})
        git('add', '-f', '.env')
        with self.assertRaises(ValueError): self.save({'SEARCH_API_KEY': 'new-fixture'})

    def test_settings_http_never_echoes_invalid_secrets_and_requires_csrf(self):
        app = create_web_app(WebSettings(data_dir=self.root/'data'), Settings('', '', frozenset(), '', '', '', ''),
            start_workers=False, settings_store=self.store)
        with TestClient(app, base_url='http://127.0.0.1:8080') as client:
            self.assertEqual(client.get('/api/settings/services').status_code, 401)
            token = client.get('/api/bootstrap').json()['csrf_token']
            headers = {'x-csrf-token': token}
            self.assertEqual(client.patch('/api/settings/services', json={}).status_code, 403)
            payload = {'version': self.store.status()['version'], 'changes': {'SEARCH_API_KEY': 'DO_NOT_ECHO\nBAD'}}
            response = client.patch('/api/settings/services', headers=headers, json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('DO_NOT_ECHO', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(client.post('/api/settings/services/test', headers=headers,
                json={'service': 'llm', 'url': 'https://other.example'}).status_code, 400)
            with patch.object(self.store, 'test', side_effect=RuntimeError('DO_NOT_ECHO')):
                response = client.post('/api/settings/services/test', headers=headers, json={'service': 'search'})
                self.assertNotIn('DO_NOT_ECHO', response.text)

    def test_restart_status_uses_new_saved_values(self):
        self.save({'SEARCH_API_KEY': 'fixture'})
        fresh = EnvSettingsStore(self.root, inherited={})
        self.assertFalse(fresh.status()['restart_required'])
        self.assertTrue(fresh.status()['fields']['SEARCH_API_KEY']['runtime_configured'])
