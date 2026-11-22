import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from adapters.web_app import create_web_app
from core.settings import Settings
from core.web_settings import WebSettings
from infrastructure.env_settings_store import EnvSettingsStore, SettingsConflict
from infrastructure.memory_store import MemoryStore
from infrastructure.public_http import PublicHTTPError
from test_model_config import configuration


def probe_client():
    def create(**kwargs):
        content='OK';tools=None
        if kwargs.get('response_format'):content='{"ok":true}'
        if kwargs.get('tools'):tools=[SimpleNamespace(function=SimpleNamespace(name='probe_echo',arguments='{"ok":true}'))]
        if any(p.get('type')=='image_url' for m in kwargs.get('messages',[]) for p in m.get('content',[]) if isinstance(p,dict)):content='white'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,tool_calls=tools),finish_reason='stop')],
            usage=SimpleNamespace(prompt_tokens=20,completion_tokens=2))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=create))),close=Mock())


class ModelSettingsTests(unittest.TestCase):
    def test_saved_price_restart_new_call_and_conversation_report_share_the_frozen_rate(self):
        from core.model_config import parse_config
        from core.model_budget import MODEL_EVENT,model_call_context
        from infrastructure.model_gateway import ModelGateway,conversation_usage_report,usage_report
        config=configuration();config['roles']={'regular':'first-model'}
        saved=self.save_models(config)
        self.assertTrue(saved['model_config']['pending_restart'])
        restarted=EnvSettingsStore(self.root,inherited={})
        self.assertFalse(restarted.status()['model_config']['pending_restart'])
        store=MemoryStore(self.root/'restart-price.db')
        token=MODEL_EVENT.set('web:group:price-flow:one')
        try:
            with patch.dict(os.environ,{'AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0'}):
                first=ModelGateway(store,configuration=parse_config(dotenv_values(self.path)['LLM_CONFIG_JSON']),
                    client_factory=lambda **kw:probe_client())
                with model_call_context('trip_plan'):first.create(messages=[])
                config['profiles'][0]['pricing']={'input':7,'cached':None,'output':11,'currency':'USD'}
                changed=self.save_models(config)
                self.assertTrue(changed['model_config']['pending_restart'])
                second=ModelGateway(store,configuration=parse_config(dotenv_values(self.path)['LLM_CONFIG_JSON']),
                    client_factory=lambda **kw:probe_client())
                with model_call_context('trip_plan'):second.create(messages=[])
                active=EnvSettingsStore(self.root,inherited={}).status()['model_config']
                self.assertTrue(active['runtime_pricing'][0]['complete'])
                self.assertEqual(active['runtime_pricing'][0]['pricing']['input'],7)
                with patch.dict(os.environ,{'LLM_CONFIG_JSON':dotenv_values(self.path)['LLM_CONFIG_JSON']}):
                    settings_report=usage_report(store)
                    conversation=conversation_usage_report(store,'price-flow','owner')
        finally:MODEL_EVENT.reset(token)
        self.assertAlmostEqual(settings_report['totals']['estimated_cost'],.000024+.000162)
        self.assertEqual(conversation['totals']['estimated_cost'],settings_report['totals']['estimated_cost'])
        with store._connect() as db:
            rows=db.execute('SELECT input_price_per_m,estimated_cost FROM model_calls ORDER BY started_at').fetchall()
        self.assertEqual([r['input_price_per_m'] for r in rows],[1,7])
        self.assertEqual([r['estimated_cost'] for r in rows],[.000024,.000162])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'project';self.root.mkdir();self.path=self.root/'.env'
        self.path.write_text('LLM_MODEL_ID=original\nLLM_BASE_URL=https://old.example/v1\nLLM_API_KEY=original-secret\nLLM_INTENT_MODEL_ID=intent\nLLM_SUMMARY_MODEL_ID=summary\n',encoding='utf-8')
        self.store=EnvSettingsStore(self.root,inherited={})

    def save_models(self,value,changes=None):
        with patch('infrastructure.env_settings_store.public_address'):
            return self.store.save(self.store.status()['version'],changes or {},model_config=value)

    def test_legacy_projection_keeps_overrides_without_writing(self):
        before=self.path.read_bytes();state=self.store.status()['model_config']
        self.assertTrue(state['legacy']);self.assertEqual(len(state['profiles']),3)
        self.assertNotEqual(state['purpose_overrides']['intent_parse'],state['purpose_overrides']['conversation_summary'])
        self.assertEqual(self.path.read_bytes(),before);self.assertNotIn('original-secret',json.dumps(state))

    def test_structured_save_redacts_then_retains_replaces_and_clears_each_key(self):
        state=self.save_models(configuration());self.assertFalse(state['model_config']['legacy'])
        self.assertEqual(state['fields']['LLM_CONFIG_JSON']['value'],'')
        self.assertFalse(state['fields']['LLM_API_KEY']['editable'])
        self.assertNotIn('-secret',json.dumps(state))
        value=state['model_config'];value={k:value[k] for k in configuration()}
        value['connections'][0]['api_key']=''
        value['connections'][1]['api_key']='replacement'
        value['connections'][2]['api_key']=None
        self.save_models(value)
        stored=json.loads(dotenv_values(self.path)['LLM_CONFIG_JSON'])['connections']
        self.assertEqual([c['api_key'] for c in stored],['first-secret','replacement',''])
        self.assertEqual(dotenv_values(self.path)['LLM_API_KEY'],'original-secret')

    def test_rebinding_or_invalid_reference_is_atomic(self):
        self.save_models(configuration());before=self.path.read_bytes()
        value=configuration();value['connections'][0].update(base_url='https://new.example/v1',api_key='')
        with self.assertRaisesRegex(ValueError,'新地址'):self.save_models(value,{'AI_BUDGET_TIER':'economy'})
        self.assertEqual(self.path.read_bytes(),before)
        value=configuration();value['connections'].pop()
        with self.assertRaises(ValueError):self.save_models(value)
        self.assertEqual(self.path.read_bytes(),before)

    def test_legacy_fields_are_read_only_after_migration_and_explicit_clear_never_reactivates_them(self):
        self.save_models(configuration())
        with self.assertRaises(ValueError):self.store.save(self.store.status()['version'],{'LLM_API_KEY':'other'})
        state=self.save_models(None)
        self.assertFalse(state['model_config']['configured']);self.assertFalse(state['model_config']['legacy'])
        self.assertEqual(dotenv_values(self.path)['LLM_API_KEY'],'original-secret')

    def test_whole_block_inherited_source_is_not_merged_with_project_credentials(self):
        value=configuration();controlled=EnvSettingsStore(self.root,inherited={'LLM_CONFIG_JSON':json.dumps(value)})
        state=controlled.status()['model_config'];self.assertEqual(state['source'],'environment')
        self.assertEqual(len(state['connections']),3)
        self.assertNotIn('legacy',{c['id'] for c in state['connections']})
        with patch('infrastructure.env_settings_store.public_address'):
            controlled.save(controlled.status()['version'],{},model_config=value)
        self.assertEqual(controlled.status()['model_config']['source'],'project')

    def test_invalid_present_block_disables_models_and_can_be_replaced_without_old_key_fallback(self):
        self.path.write_text(self.path.read_text()+"LLM_CONFIG_JSON='{broken'\n",encoding='utf-8')
        self.assertFalse(self.store.status()['model_config']['valid'])
        value=configuration()
        for row in value['connections']:row.pop('api_key')
        state=self.save_models(value)
        self.assertFalse(state['model_config']['configured'])
        self.assertTrue(all(not c['key_configured'] for c in state['model_config']['connections']))

    def test_canonical_block_size_is_checked_before_atomic_save(self):
        value={'schema_version':1,'connections':[{'id':'c'+str(i),'name':'n'*80,
            'base_url':'https://example.com/v1','api_key':'k'*4096} for i in range(12)],
            'profiles':[{'id':'p'+str(i),'connection_id':'c'+str(i%12),'model_id':'m'*200} for i in range(36)],
            'roles':{'regular':'p0'}}
        self.assertLess(len(json.dumps(value).encode()),65536)
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.save_models(value)
        self.assertEqual(self.path.read_bytes(),before)


class ServiceSettingsTests(unittest.TestCase):
    def test_rail_config_is_write_only_retains_and_clears_as_one_block(self):
        raw=json.dumps({'enabled':True,'transport':'sse','endpoint':'https://rail.example/private/sse','bearer_token':'rail-secret'})
        with patch('infrastructure.env_settings_store.public_address'):
            result=self.save({'RAIL_MCP_CONFIG':raw})
        self.assertEqual(result['fields']['RAIL_MCP_CONFIG']['value'],'')
        self.assertTrue(result['rail_mcp']['enabled'])
        self.assertNotIn('rail-secret',json.dumps(result))
        self.assertNotIn('/private',json.dumps(result))
        self.save({'RAIL_MCP_CONFIG':''})
        self.assertEqual(dotenv_values(self.path)['RAIL_MCP_CONFIG'],raw)
        self.save({'RAIL_MCP_CONFIG':None})
        self.assertFalse(self.store.status()['rail_mcp']['enabled'])

    def test_legacy_rail_probe_entry_remains_available(self):
        import asyncio
        from unittest.mock import AsyncMock
        probe=AsyncMock(return_value={'status':'ok','detail':'contract only'})
        with patch('infrastructure.service_probes.probe_rail_config',probe):
            self.assertEqual(self.store.test('rail')['status'],'ok')
        probe.assert_awaited_once()

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
        client=probe_client();model=client.chat.completions.create
        with patch('infrastructure.service_probes.create_model_client', return_value=client), \
                patch('infrastructure.service_probes.SearchClient.search',return_value=[]) as search, \
                patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{'city':'北京'}]}) as amap:
            report = self.store.test_all(store)
        self.assertEqual(report['summary'], {'ok':6,'failed':0,'skipped':2})
        self.assertEqual(model.call_count,4)  # Text, JSON, tool and image contracts are independent.
        contents = [call.kwargs['messages'][0]['content'] for call in model.call_args_list]
        self.assertEqual(sum(any(part['type']=='image_url' for part in content) for content in contents),1)
        search.assert_called_once()
        amap.assert_called_once_with('/v3/weather/weatherInfo',{'city':'110100','extensions':'base'})
        self.assertEqual({r['capability'] for r in report['results'] if r['service']=='model'},{'text','json','tools','vision'})
        with store._connect() as db:
            rows = db.execute('SELECT purpose,prompt_tokens,completion_tokens FROM model_calls').fetchall()
        self.assertEqual(len(rows),4)
        self.assertTrue(all(row['purpose']=='service_probe' and row['prompt_tokens']==20 for row in rows))
        for secret in ('old-model-fixture','old-map-fixture','search-secret'):
            self.assertNotIn(secret,json.dumps(report))

    def test_all_services_isolates_failures_and_does_not_return_provider_errors(self):
        self.save({'LLM_MODEL_ID':'test-model','SEARCH_API_KEY':'search-secret'})
        client=probe_client();client.chat.completions.create.side_effect=PublicHTTPError('unauthorized')
        with patch('infrastructure.service_probes.create_model_client',return_value=client), \
                patch('infrastructure.service_probes.SearchClient.search',side_effect=RuntimeError('secret-provider-error')), \
                patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{}]}):
            report = self.store.test_all(MemoryStore(self.root/'probe.db'))
        self.assertEqual(report['summary'],{'ok':1,'failed':5,'skipped':2})
        self.assertIn('认证失败',report['results'][0]['detail'])
        self.assertNotIn('secret-provider-error',json.dumps(report))

    def test_all_services_tests_each_saved_purpose_model_without_runtime_fallback(self):
        names={'LLM_MODEL_ID':'saved-default','LLM_INTENT_MODEL_ID':'saved-intent',
               'LLM_SUMMARY_MODEL_ID':'saved-summary','LLM_RESEARCH_MODEL_ID':'saved-research',
               'LLM_VISION_MODEL_ID':'saved-vision'}
        self.save({**names,'SEARCH_API_KEY':'search-secret'})
        self.save({'LLM_INPUT_PRICE_PER_M':'2','LLM_OUTPUT_PRICE_PER_M':'10'})
        client=probe_client();model=client.chat.completions.create
        store=MemoryStore(self.root/'probe.db')
        with patch.dict(os.environ,{'LLM_INTENT_MODEL_ID':'wrong-runtime-model',
                'LLM_INPUT_PRICE_PER_M':'2','LLM_OUTPUT_PRICE_PER_M':'10','LLM_MODEL_PRICES':'[]',
                'AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0'}), \
                patch('infrastructure.service_probes.create_model_client',return_value=client), \
                patch('infrastructure.service_probes.SearchClient.search',return_value=[]), \
                patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{}]}):
            report=self.store.test_all(store)
        self.assertEqual(report['summary'],{'ok':9,'failed':0,'skipped':2})
        self.assertEqual({call.kwargs['model'] for call in model.call_args_list},set(names.values()))
        with store._connect() as db:
            costs={row['model_id']:row['estimated_cost'] for row in db.execute('SELECT model_id,estimated_cost FROM model_calls')}
        self.assertIsNotNone(costs['saved-default'])
        self.assertIsNone(costs['saved-intent'])  # Unknown purpose-model rates must not use default-model prices.

    def test_all_services_skips_unconfigured_targets_without_network(self):
        self.save({'LLM_API_KEY':None,'AMAP_API_KEY':None})
        with patch('infrastructure.service_probes.create_model_client') as model, \
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

    def test_all_services_includes_enabled_rail_and_wendao_once(self):
        from unittest.mock import AsyncMock
        with patch('infrastructure.env_settings_store.public_address'):
            self.save({'RAIL_MCP_CONFIG':json.dumps({'enabled':True,'endpoint':'https://rail.example/mcp'}),
                'WENDAO_ENABLED':'true','WENDAO_API_KEY':'wendao-fixture','LLM_MODEL_ID':'shared'})
        client=probe_client();rail=AsyncMock(return_value={'status':'ok','detail':'铁路合同通过；未查车票。'})
        with patch('infrastructure.service_probes.create_model_client',return_value=client),\
             patch('infrastructure.service_probes.probe_rail_config',rail),\
             patch('infrastructure.service_probes.WendaoClient.query',return_value={'status':'completed'}) as wendao,\
             patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{}]}):
            report=self.store.test_all(MemoryStore(self.root/'all.db'))
        self.assertEqual({r['service'] for r in report['results']},{'model','search','amap','rail','wendao'})
        self.assertEqual(next(r for r in report['results'] if r['service']=='rail')['status'],'ok')
        self.assertEqual(next(r for r in report['results'] if r['service']=='wendao')['status'],'ok')
        rail.assert_awaited_once();wendao.assert_called_once()
        self.assertEqual(wendao.call_args.kwargs['timeout'],20)
        self.assertIn('一句话',wendao.call_args.args[2])

    def test_disabled_services_do_not_submit_and_failures_are_independent(self):
        from unittest.mock import AsyncMock
        self.save({'WENDAO_API_KEY':'saved-but-disabled'})
        with patch('infrastructure.service_probes.probe_rail_config',new_callable=AsyncMock) as rail,\
             patch('infrastructure.service_probes.WendaoClient.query') as wendao,\
             patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{}]}):
            report=self.store.test_all(MemoryStore(self.root/'disabled.db'))
        rail.assert_not_awaited();wendao.assert_not_called()
        self.assertTrue(all(r['status']=='skipped' for r in report['results'] if r['service'] in {'rail','wendao'}))

    def test_invalid_model_and_rail_configuration_do_not_block_wendao(self):
        self.path.write_text(self.path.read_text()+"\nLLM_CONFIG_JSON='{invalid'\nRAIL_MCP_CONFIG='{invalid'\nWENDAO_ENABLED=true\nWENDAO_API_KEY=fixture\n",encoding='utf-8')
        with patch('infrastructure.service_probes.WendaoClient.query',return_value={'status':'completed'}) as wendao,\
             patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{}]}):
            report=self.store.test_all(MemoryStore(self.root/'invalid.db'))
        rows={r['service']:r for r in report['results']}
        self.assertEqual(rows['model']['status'],'failed');self.assertEqual(rows['rail']['status'],'failed')
        self.assertEqual(rows['wendao']['status'],'ok');wendao.assert_called_once()

    def test_rail_and_wendao_errors_never_leak_credentials_or_retry(self):
        from unittest.mock import AsyncMock
        with patch('infrastructure.env_settings_store.public_address'):
            self.save({'RAIL_MCP_CONFIG':json.dumps({'enabled':True,'endpoint':'https://rail.example/private'}),
                'WENDAO_ENABLED':'true','WENDAO_API_KEY':'private-token'})
        rail=AsyncMock(side_effect=RuntimeError('private-token https://rail.example/private'))
        with patch('infrastructure.service_probes.probe_rail_config',rail),\
             patch('infrastructure.service_probes.WendaoClient.query',return_value={'status':'result_unknown','error_code':'read_timeout'}) as wendao,\
             patch('infrastructure.service_probes.AmapClient._get',return_value={'lives':[{}]}):
            report=self.store.test_all(MemoryStore(self.root/'failed.db'))
        rows={r['service']:r for r in report['results']}
        self.assertEqual(rows['amap']['status'],'ok');self.assertEqual(rows['rail']['status'],'failed')
        self.assertIn('结果未知',rows['wendao']['detail']);self.assertNotIn('private-token',json.dumps(report))
        self.assertNotIn('https://rail.example/private',json.dumps(report));wendao.assert_called_once()

    def test_wendao_probe_reuses_the_real_single_submission_contract(self):
        from infrastructure.wendao_client import WendaoClient
        self.save({'WENDAO_ENABLED':'true','WENDAO_API_KEY':'probe-fixture-token','LLM_API_KEY':None,'AMAP_API_KEY':None})
        transport=Mock(return_value=(b'{"result":{"content":"ok"}}','','application/json'))
        store=MemoryStore(self.root/'wendao-probe.db')
        with patch('infrastructure.service_probes.WendaoClient',side_effect=lambda token,enabled:WendaoClient(token,enabled,transport=transport)):
            report=self.store.test_all(store)
        self.assertEqual(next(r for r in report['results'] if r['service']=='wendao')['status'],'ok')
        transport.assert_called_once();self.assertEqual(transport.call_args.kwargs['max_attempts'],1)
        with store._connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM wendao_calls').fetchone()[0],1)
