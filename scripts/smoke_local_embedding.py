"""Exercise real local encoding and runtime wiring against an isolated database."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.runtime_factory import build_runtime
from core.settings import Settings
from core.model_budget import MODEL_EVENT
from infrastructure.memory_store import MemoryStore
from infrastructure.execution_report import export_timeline


def smoke(args):
    env = {'AI_VECTOR_ENABLED': 'true', 'AI_EMBEDDING_MODEL_DIR': str(args.model_dir.resolve()),
           'AI_EMBEDDING_MIN_SIMILARITY': '0.5', 'AI_SUMMARY_ENABLED': 'false'}
    with tempfile.TemporaryDirectory(prefix='local-embedding-smoke-') as folder, patch.dict('os.environ', env), \
            patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
        store = MemoryStore(Path(folder) / 'test.db')
        runtime = build_runtime(Settings('', '', frozenset(), '', '', '', ''), platform='onebot',
            transport=SimpleNamespace(), reply_renderer=SimpleNamespace(), group_allowed=lambda _: True, store=store)
        index = store.semantic_index
        assert index is not None
        text = '湖边平整步道适合轮椅通行，每隔约五分钟步行距离设有长椅；山顶路线有连续石阶。'
        store.add_document('scope', 'owner', 'fixture.md', 'fixture', text, [text])
        store.inbox.submit('local-test', 'onebot', 'scope', 'owner', {})
        index.update('scope')
        token = MODEL_EVENT.set('local-test')
        try:
            with store._connect() as db:
                found = store._select_document_chunks(db, 'scope', '同行者腿脚不方便又需要经常坐下来歇一歇，应当选择什么路段？',
                                                       allow_low_relevance=False)
        finally:
            MODEL_EVENT.reset(token)
        assert found
        assert index.search('other-scope', '轮椅') == []
        embedder = index.embedder
        session = embedder.session
        calls = []
        def run(*arguments):
            calls.append(len(arguments[1]['input_ids'][0]))
            return session.run(*arguments)
        embedder.session = SimpleNamespace(run=run, get_inputs=session.get_inputs)
        vector = embedder(['摄影中的顺光、侧光和逆光。' * 100 + '末尾提供轮椅服务。'])[0]
        assert len(vector) == 512 and len(calls) > 1 and max(calls) <= 512
        long_windows = list(calls)
        with store._connect() as db:
            db.execute("UPDATE document_chunks SET content='已更新的资料' WHERE document_id IN (SELECT id FROM documents WHERE group_openid='scope')")
        assert index.search('scope', '轮椅') == []
        index.update('scope')
        with store._connect() as db:
            db.execute("DELETE FROM documents WHERE group_openid='scope'")
            assert db.execute('SELECT count(*) FROM semantic_chunk_vectors').fetchone()[0] == 0
        assert index.search('scope', '轮椅') == []
        trace = export_timeline(store.database_path, 'local-test')
        assert any(row['stage'] == 'retrieval' and row['details']['status'] == 'completed' for row in trace['timeline'])
        report = {'mode': 'real-local-encoder-runtime-isolated-smoke', 'passed': True, 'dimensions': len(vector),
                  'model': embedder.model_id, 'retrieval_returned': len(found), 'long_document_windows': long_windows,
                  'scope_isolation': True, 'changed_source_excluded': True, 'deleted_vectors_removed': True,
                  'runtime_wired': True, 'retrieval_diagnostic_recorded': True,
                  'network_requests': 0, 'paid_model_requests': 0, 'production_database_accessed': False}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    smoke(parser.parse_args())
