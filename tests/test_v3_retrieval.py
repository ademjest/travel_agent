from pathlib import Path
import tempfile
import unittest
import os
import json
from types import SimpleNamespace
from unittest.mock import patch

from core.retrieval_metrics import retrieval_metrics, reciprocal_rank_fusion
from infrastructure.memory_store import MemoryStore
from infrastructure.semantic_index import SemanticIndex


class RetrievalTests(unittest.TestCase):
    def test_recall_is_not_hit_rate(self):
        metrics=retrieval_metrics([1,9],{1,2},5)
        self.assertEqual(metrics['recall'],.5)
        self.assertTrue(metrics['hit'])

    def test_no_answer_reports_false_positive_separately(self):
        metrics=retrieval_metrics([9],set(),5)
        self.assertIsNone(metrics['recall'])
        self.assertTrue(metrics['false_positive'])

    def test_rank_fusion_prefers_agreement_without_mixing_raw_scores(self):
        self.assertEqual(reciprocal_rank_fusion([[1,2],[2,3]])[0],2)

    def test_semantic_namespace_deletion_and_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'v.db')
            store.add_document('a','u','a.md','hash-a','博物馆',['博物馆'])
            store.add_document('b','u','b.md','hash-b','博物馆',['博物馆'])
            def embed(texts):
                return [[1.,0.] for _ in texts]
            index=SemanticIndex(store,embed,'toy',2)
            index.update('a')
            hits=index.search('a','馆')
            self.assertEqual(len(hits),1)
            namespace2=SemanticIndex(store,embed,'toy-v2',2)
            self.assertNotEqual(index.namespace,namespace2.namespace)
            with store._connect() as db:
                db.execute("DELETE FROM documents WHERE group_openid='a'")
            self.assertEqual(index.search('a','馆'),[])

    def test_embedding_dimension_error_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'v.db')
            store.add_document('a','u','a.md','hash-a','正文',['正文'])
            index=SemanticIndex(store,lambda texts:[[1.,0.] for _ in texts],'toy',2)
            index.update('a')
            index.embedder=lambda texts:[[1.] for _ in texts]
            with self.assertRaises(ValueError):
                index.search('a','query')

    def test_embedding_uses_gateway_price_snapshot_and_content_free_ledger(self):
        from infrastructure.model_gateway import ModelGateway
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'v.db')
            client=SimpleNamespace(embeddings=SimpleNamespace(create=lambda **kw:SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=12),data=[SimpleNamespace(embedding=[1.,0.])])) )
            gateway=ModelGateway(store,client)
            prices=[{'provider':'test','model':'embed','input':1,'output':0,'currency':'USD'}]
            with patch.dict(os.environ,{'LLM_PROVIDER_ALIAS':'test','LLM_MODEL_PRICES':json.dumps(prices)}):
                gateway.embeddings.create(model='embed',input=['authorized source'])
            with store._connect() as db:
                row=db.execute('SELECT * FROM model_usage_ledger').fetchone()
                self.assertEqual((row['purpose'],row['consumed'],row['status']),('retrieval_embedding',12,'settled'))
                self.assertAlmostEqual(row['estimated_cost'],.000012)
                self.assertNotIn('authorized source',str(dict(row)))
