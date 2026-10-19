import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from infrastructure.memory_store import MemoryStore


class LocalVectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'vectors.db')
        self.store.add_document('scope', 'owner', 'museum.md', 'vector-doc',
                                '武汉博物馆旅行资料', ['武汉省级博物馆需要提前核对预约规则。'])

    def test_vector_index_is_disabled_by_default(self):
        self.store.build_document_context('scope', '武汉博物馆')
        with self.store._connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_chunk_vectors'").fetchone()
        self.assertIsNone(exists)

    def test_enabled_vector_index_is_local_versioned_and_rebuildable(self):
        with patch.dict(os.environ, {'AI_VECTOR_ENABLED': 'true'}):
            context = self.store.build_document_context('scope', '武汉博物馆预约')
        self.assertIn('博物馆', context)
        with self.store._connect() as db:
            row = db.execute('SELECT model_id,content_hash FROM document_chunk_vectors').fetchone()
        self.assertEqual(row['model_id'], 'hash-ngram-256-v1')
        self.assertEqual(len(row['content_hash']), 64)


if __name__ == '__main__':
    unittest.main()
