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
from infrastructure.memory_store import MemoryStore
from infrastructure.public_http import PublicHTTPError


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
        self.assertTrue(controlled.status()['fields']['LLM_API_KEY']['editable'])
        result = controlled.save(controlled.status()['version'], {'LLM_API_KEY': 'new'})
        self.assertEqual(result['fields']['LLM_API_KEY']['source'], 'project')
        self.assertTrue(result['fields']['LLM_API_KEY']['overrides_environment'])
        self.assertTrue(result['fields']['LLM_API_KEY']['pending_restart'])
        version = self.store.status()['version']
        self.path.write_text(self.path.read_text() + 'ADDED=other\n')
        with self.assertRaises(SettingsConflict): self.store.save(version, {'SEARCH_API_KEY': 'fixture'})

    def test_environment_override_reloads_and_keeps_untouched_environment_fields(self):
        inherited = {'LLM_API_KEY': 'inherited-model', 'AMAP_API_KEY': 'inherited-map',
                     'LLM_MODEL_ID': 'inherited-model-name'}
        controlled = EnvSettingsStore(self.root, inherited=inherited)
        result = controlled.save(controlled.status()['version'], {'LLM_MODEL_ID': 'replacement-model'})
        self.assertEqual(result['fields']['LLM_MODEL_ID']['value'], 'replacement-model')
        self.assertEqual(result['fields']['AMAP_API_KEY']['source'], 'environment')
        fresh = EnvSettingsStore(self.root, inherited=inherited)
        self.assertFalse(fresh.status()['restart_required'])
        self.assertEqual(fresh.runtime['LLM_MODEL_ID'], 'replacement-model')
        self.assertEqual(fresh.runtime['LLM_API_KEY'], 'inherited-model')
        self.assertEqual(result['fields']['LLM_API_KEY']['value'], '')

    def test_environment_key_can_be_cleared_without_falling_back_after_restart(self):
        inherited = {'LLM_API_KEY': 'inherited-secret'}
        controlled = EnvSettingsStore(self.root, inherited=inherited)
        result = controlled.save(controlled.status()['version'], {'LLM_API_KEY': None})
        self.assertFalse(result['fields']['LLM_API_KEY']['configured'])
        self.assertEqual(EnvSettingsStore(self.root, inherited=inherited).runtime['LLM_API_KEY'], '')

    def test_changing_environment_url_still_requires_new_key(self):
        controlled = EnvSettingsStore(self.root, inherited={'LLM_BASE_URL': 'https://original.example/v1',
                                                          'LLM_API_KEY': 'inherited-secret'})
        with patch('infrastructure.env_settings_store.public_address'):
            with self.assertRaisesRegex(ValueError, '必须重新输入'):
                controlled.save(controlled.status()['version'], {'LLM_BASE_URL': 'https://new.example/v1'})

    def test_startup_applies_saved_web_overrides_to_actual_environment(self):
        inherited = {'LLM_API_KEY': 'inherited-model', 'LLM_MODEL_ID': 'inherited-name',
                     'AMAP_API_KEY': 'inherited-map'}
        controlled = EnvSettingsStore(self.root, inherited=inherited)
        controlled.save(controlled.status()['version'], {'LLM_API_KEY': 'replacement-key',
                                                        'LLM_MODEL_ID': 'replacement-name'})
        with patch.dict(os.environ, inherited), patch('core.web_settings.PROJECT_ROOT', self.root), \
                patch('infrastructure.env_settings_store.START_ENV', inherited):
            WebSettings.from_env()
            self.assertEqual(os.environ['LLM_API_KEY'], 'replacement-key')
            self.assertEqual(os.environ['LLM_MODEL_ID'], 'replacement-name')
            self.assertEqual(os.environ['AMAP_API_KEY'], 'inherited-map')

    def test_override_record_cannot_set_unrelated_environment_variables(self):
        self.path.write_text(self.path.read_text() + '\nWEB_SETTINGS_OVERRIDE_KEYS=\'["PATH"]\'\n')
        with self.assertRaisesRegex(ValueError, '不支持的字段'):
            EnvSettingsStore(self.root, inherited={})

    def test_http_replaces_environment_credentials_and_reports_pending_restart(self):
        inherited = {'LLM_API_KEY': 'startup-model-fixture', 'AMAP_API_KEY': 'startup-map-fixture',
                     'LLM_BASE_URL': 'https://original.example/v1', 'LLM_MODEL_ID': 'original-model'}
        controlled = EnvSettingsStore(self.root, inherited=inherited)
        app = create_web_app(WebSettings(data_dir=self.root/'data'), Settings('', '', frozenset(), '', '', '', ''),
            start_workers=False, settings_store=controlled)
        with TestClient(app, base_url='http://127.0.0.1:8080') as client:
            token = client.get('/api/bootstrap').json()['csrf_token']
            headers = {'x-csrf-token': token}
            initial = client.get('/api/settings/services').json()
            for key in inherited:
                self.assertTrue(initial['fields'][key]['editable'])
            with patch('infrastructure.env_settings_store.public_address'):
                rejected = client.patch('/api/settings/services', headers=headers, json={
                    'version':initial['version'], 'changes':{'LLM_BASE_URL':'https://new.example/v1'}})
                self.assertEqual(rejected.status_code, 400)
                self.assertIn('必须重新输入', rejected.json()['detail'])
                response = client.patch('/api/settings/services', headers=headers, json={
                    'version':initial['version'], 'changes':{'LLM_BASE_URL':'https://new.example/v1',
                        'LLM_MODEL_ID':'replacement-model', 'LLM_API_KEY':'replacement-model-fixture',
                        'AMAP_API_KEY':'replacement-map-fixture'}})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['restart_required'])
            for key in inherited:
                field = response.json()['fields'][key]
                self.assertTrue(field['pending_restart'])
                self.assertTrue(field['overrides_environment'])
                self.assertEqual(field['source'], 'project')
            for secret in ('startup-model-fixture','startup-map-fixture','replacement-model-fixture','replacement-map-fixture'):
                self.assertNotIn(secret, response.text)
            fresh = EnvSettingsStore(self.root, inherited=inherited)
            self.assertEqual(fresh.runtime['LLM_API_KEY'], 'replacement-model-fixture')
            self.assertEqual(fresh.runtime['AMAP_API_KEY'], 'replacement-map-fixture')
            self.assertFalse(fresh.status()['restart_required'])

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

    def test_budget_models_and_feature_flags_are_validated(self):
        result = self.save({'AI_BUDGET_TIER': 'economy', 'AI_DAILY_TOKEN_LIMIT': '100000',
                            'AI_SUMMARY_ENABLED': 'true', 'AI_VECTOR_ENABLED': 'false',
                            'LLM_INTENT_MODEL_ID': 'small-model', 'LLM_INPUT_PRICE_PER_M': '1.25',
                            'LLM_OUTPUT_PRICE_PER_M': '8', 'LLM_PRICE_CURRENCY': 'CNY'})
        self.assertEqual(result['fields']['AI_BUDGET_TIER']['value'], 'economy')
        self.assertEqual(result['fields']['LLM_INTENT_MODEL_ID']['value'], 'small-model')
        for changes in ({'AI_BUDGET_TIER': 'fast'}, {'AI_SUMMARY_ENABLED': 'yes'},
                        {'AI_DAILY_TOKEN_LIMIT': '-1'}, {'LLM_INPUT_PRICE_PER_M': 'free'},
                        {'LLM_PRICE_CURRENCY': '人民币'}):
            with self.assertRaises(ValueError):
                self.save(changes)

    def test_default_price_currency_is_usd(self):
        self.assertEqual(self.store.status()['fields']['LLM_PRICE_CURRENCY']['value'],'USD')

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
            usage = client.get('/api/settings/model-usage')
            self.assertEqual(usage.status_code, 200)
            self.assertEqual(usage.json()['totals']['calls'], 0)

    def test_restart_status_uses_new_saved_values(self):
        self.save({'SEARCH_API_KEY': 'fixture'})
        fresh = EnvSettingsStore(self.root, inherited={})
        self.assertFalse(fresh.status()['restart_required'])
        self.assertTrue(fresh.status()['fields']['SEARCH_API_KEY']['runtime_configured'])

    def test_all_services_deduplicates_models_and_records_probe_usage(self):
        self.save({'LLM_MODEL_ID':'shared-model', 'LLM_INTENT_MODEL_ID':'shared-model',
                   'LLM_SUMMARY_MODEL_ID':'shared-model', 'SEARCH_API_KEY':'search-secret'})
        store = MemoryStore(self.root/'probe.db')
        response = json.dumps({'choices':[{'message':{'content':'OK'}}],
            'usage':{'prompt_tokens':20,'completion_tokens':2,'prompt_tokens_details':{'cached_tokens':0}}}).encode()
        with patch('infrastructure.service_probes.request_public', return_value=(response,'','application/json')) as model, \
                patch('infrastructure.service_probes.SearchClient.search',return_value=[]) as search, \
                patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{'city':'北京'}]}) as amap:
            report = self.store.test_all(store)
        self.assertEqual(report['summary'], {'ok':4,'failed':0,'skipped':0})
        self.assertEqual(model.call_count,2)  # One text request and one image-input request.
        contents = [call.kwargs['payload']['messages'][0]['content'] for call in model.call_args_list]
        self.assertEqual(sum(any(part['type']=='image_url' for part in content) for content in contents),1)
        search.assert_called_once()
        amap.assert_called_once_with('/v3/weather/weatherInfo',{'city':'110100','extensions':'base'})
        self.assertIn('意图解析', report['results'][0]['label'])
        with store._connect() as db:
            rows = db.execute('SELECT purpose,prompt_tokens,completion_tokens FROM model_calls').fetchall()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row['purpose']=='service_probe' and row['prompt_tokens']==20 for row in rows))
        for secret in ('old-model-fixture','old-map-fixture','search-secret'):
            self.assertNotIn(secret,json.dumps(report))

    def test_all_services_isolates_failures_and_does_not_return_provider_errors(self):
        self.save({'LLM_MODEL_ID':'test-model','SEARCH_API_KEY':'search-secret'})
        with patch('infrastructure.service_probes.request_public',side_effect=PublicHTTPError('unauthorized')), \
                patch('infrastructure.service_probes.SearchClient.search',side_effect=RuntimeError('secret-provider-error')), \
                patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{}]}):
            report = self.store.test_all(MemoryStore(self.root/'probe.db'))
        self.assertEqual(report['summary'],{'ok':1,'failed':3,'skipped':0})
        self.assertIn('认证失败',report['results'][0]['detail'])
        self.assertNotIn('secret-provider-error',json.dumps(report))

    def test_all_services_tests_each_saved_purpose_model_without_runtime_fallback(self):
        names={'LLM_MODEL_ID':'saved-default','LLM_INTENT_MODEL_ID':'saved-intent',
               'LLM_SUMMARY_MODEL_ID':'saved-summary','LLM_RESEARCH_MODEL_ID':'saved-research',
               'LLM_VISION_MODEL_ID':'saved-vision'}
        self.save({**names,'SEARCH_API_KEY':'search-secret'})
        response=json.dumps({'choices':[{'message':{'content':'OK'}}],
                             'usage':{'prompt_tokens':20,'completion_tokens':2}}).encode()
        store=MemoryStore(self.root/'probe.db')
        with patch.dict(os.environ,{'LLM_INTENT_MODEL_ID':'wrong-runtime-model',
                'LLM_INPUT_PRICE_PER_M':'2','LLM_OUTPUT_PRICE_PER_M':'10','LLM_MODEL_PRICES':'[]',
                'AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0'}), \
                patch('infrastructure.service_probes.request_public',return_value=(response,'','application/json')) as model, \
                patch('infrastructure.service_probes.SearchClient.search',return_value=[]), \
                patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{}]}):
            report=self.store.test_all(store)
        self.assertEqual(report['summary'],{'ok':7,'failed':0,'skipped':0})
        self.assertEqual({call.kwargs['payload']['model'] for call in model.call_args_list},set(names.values()))
        with store._connect() as db:
            costs={row['model_id']:row['estimated_cost'] for row in db.execute('SELECT model_id,estimated_cost FROM model_calls')}
        self.assertIsNotNone(costs['saved-default'])
        self.assertIsNone(costs['saved-intent'])  # Unknown purpose-model rates must not use default-model prices.

    def test_all_services_skips_unconfigured_targets_without_network(self):
        self.save({'LLM_API_KEY':None,'AMAP_API_KEY':None})
        with patch('infrastructure.service_probes.request_public') as model, \
                patch('infrastructure.service_probes.SearchClient.search') as search, \
                patch('infrastructure.service_probes.AmapClient._get') as amap:
            report = self.store.test_all(MemoryStore(self.root/'probe.db'))
        self.assertEqual(report['summary'],{'ok':0,'failed':0,'skipped':4})
        model.assert_not_called(); search.assert_not_called(); amap.assert_not_called()

    def test_all_services_http_requires_csrf_and_rejects_overlapping_runs(self):
        app = create_web_app(WebSettings(data_dir=self.root/'data'), Settings('', '', frozenset(), '', '', '', ''),
            start_workers=False, settings_store=self.store)
        with TestClient(app,base_url='http://127.0.0.1:8080') as client:
            token = client.get('/api/bootstrap').json()['csrf_token']
            self.assertEqual(client.post('/api/settings/services/test',json={'service':'all'}).status_code,403)
            headers={'x-csrf-token':token}
            with patch.object(self.store,'test_all',return_value={'results':[],'summary':{'ok':0,'failed':0,'skipped':0}}) as probe:
                response=client.post('/api/settings/services/test',headers=headers,json={'service':'all'})
                self.assertEqual(response.status_code,200)
                probe.assert_called_once()
                self.assertEqual(client.post('/api/settings/services/test',headers=headers,
                    json={'service':'all','url':'https://other.example'}).status_code,400)
                self.assertEqual(probe.call_count,1)
            self.store._probe_lock.acquire()
            try:
                self.assertEqual(client.post('/api/settings/services/test',headers=headers,
                    json={'service':'all'}).status_code,409)
            finally:
                self.store._probe_lock.release()
