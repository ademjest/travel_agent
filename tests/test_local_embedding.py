import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from infrastructure.local_embedding import LocalEmbedding, MODEL_REPO, MODEL_REVISION, MODEL_SHA256, TOKENIZER_SHA256, configure_local_index
from infrastructure.memory_store import MemoryStore
from infrastructure.semantic_index import SemanticIndex
from infrastructure.env_settings_store import EnvSettingsStore


class LocalEmbeddingTests(unittest.TestCase):
    def test_partial_index_progress_is_a_readonly_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'test.db')
            store.add_document('scope','u','many.txt','sha','正文',[f'片段{i}' for i in range(35)])
            index=SemanticIndex(store,lambda texts:[[1.,0.] for _ in texts],'toy',2)
            index.update('scope')
            self.assertEqual(index.snapshot('scope')['index']['state'],'partial')
            self.assertEqual(index.snapshot('scope')['index']['pending'],3)
            count=index.calls
            index.snapshot('scope')
            self.assertEqual(index.calls,count)
            index.update('scope')
            self.assertEqual(index.snapshot('scope')['index']['indexed'],35)
            self.assertEqual(index.snapshot('scope')['index']['state'],'ready')

    def test_optional_index_worker_is_included_in_web_health(self):
        from fastapi.testclient import TestClient
        from adapters.web_app import create_web_app
        from core.settings import Settings
        from core.web_settings import WebSettings
        def attach(store):
            store.semantic_index = SemanticIndex(store, lambda texts: [[1., 0.] for _ in texts], 'toy', 2)
        with tempfile.TemporaryDirectory() as folder, patch('infrastructure.local_embedding.configure_local_index', side_effect=attach):
            app = create_web_app(WebSettings(data_dir=Path(folder), open_browser=False),
                                 Settings('', '', frozenset(), '', '', '', ''), start_workers=True)
            with TestClient(app, base_url='http://127.0.0.1:8080') as client:
                self.assertEqual(client.get('/health/ready').status_code, 200)
                self.assertIn('web-local-index', app.state.runtime.supervisor.snapshot())

    def test_document_and_query_use_distinct_encoding(self):
        class Encoder:
            def __call__(self, texts):
                calls.append(('document', texts))
                return [[1., 0.] for _ in texts]
            def encode_queries(self, texts):
                calls.append(('query', texts))
                return [[1., 0.] for _ in texts]
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder) / 'index.db')
            store.add_document('scope', 'u', 'a.txt', 'sha', '正文', ['正文'])
            index = SemanticIndex(store, Encoder(), 'pinned-model', 2)
            index.update('scope')
            self.assertTrue(index.search('scope', '问题'))
            self.assertEqual([call[0] for call in calls], ['document', 'query'])
            self.assertEqual(index.search('other', '问题'), [])
            self.assertEqual(len(calls), 2)

    def test_runtime_configuration_failure_uses_lexical_not_hash(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,
                {'AI_VECTOR_ENABLED': 'true', 'AI_EMBEDDING_MODEL_DIR': ''}):
            store = MemoryStore(Path(folder) / 'index.db')
            configure_local_index(store)
            store.add_document('scope', 'u', 'a.txt', 'sha', '武汉地铁', ['武汉地铁'])
            with patch('infrastructure.memory_store.vector_candidates', side_effect=AssertionError('hash must not run')):
                with store._connect() as db:
                    self.assertTrue(store._select_document_chunks(db, 'scope', '武汉地铁'))
            self.assertTrue(store.semantic_index_unavailable)

    def test_model_and_tokenizer_revision_are_validated_before_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'manifest.json'
            value = {'repo': MODEL_REPO, 'revision': MODEL_REVISION,
                     'files': {'onnx/model_quantized.onnx': MODEL_SHA256, 'tokenizer.json': TOKENIZER_SHA256}}
            path.write_text(json.dumps(value), encoding='utf-8')
            first = LocalEmbedding(folder)
            self.assertIsNone(first.session)
            value['files']['tokenizer.json'] = 'bad'
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                LocalEmbedding(folder)

    def test_encoder_failure_preserves_lexical_results(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'AI_VECTOR_ENABLED': 'true'}):
            store = MemoryStore(Path(folder) / 'index.db')
            store.add_document('scope', 'u', 'a.txt', 'sha', '武汉地铁', ['武汉地铁'])
            class BrokenIndex:
                def search(self, *args):
                    raise ImportError('missing optional dependency')
            store.semantic_index = BrokenIndex()
            with store._connect() as db:
                self.assertTrue(store._select_document_chunks(db, 'scope', '武汉地铁'))

    def test_new_documents_are_not_indexed_inside_search(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder) / 'index.db')
            store.add_document('scope', 'u', 'a.txt', 'sha', '正文', ['正文'])
            index = SemanticIndex(store, lambda texts: [[1., 0.] for _ in texts], 'toy', 2)
            self.assertEqual(index.search('scope', '正文'), [])
            self.assertEqual(index.calls, 0)
            with store._connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM semantic_chunk_vectors').fetchone()[0], 0)

    def test_model_directory_and_threshold_are_editable_and_validated(self):
        EnvSettingsStore.validate('AI_EMBEDDING_MODEL_DIR', '.models/bge-small-zh-v1.5-onnx')
        EnvSettingsStore.validate('AI_EMBEDDING_MIN_SIMILARITY', '0.5')
        for invalid in ('nan', 'inf', '-1', '1.01', 'abc'):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                EnvSettingsStore.validate('AI_EMBEDDING_MIN_SIMILARITY', invalid)
