"""Isolated lexical/BGE retrieval comparison. No chat models or production data."""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infrastructure.local_embedding import LocalEmbedding, DIMENSIONS
from infrastructure.semantic_index import SemanticIndex
from infrastructure.memory_store import MemoryStore
from core.retrieval_metrics import retrieval_metrics


class CachedEncoder:
    def __init__(self, encoder):
        self.encoder, self.cache = encoder, {}

    def encode(self, texts, query=False):
        missing = list(dict.fromkeys(text for text in texts if (query, text) not in self.cache))
        if missing:
            values = self.encoder.encode_queries(missing) if query else self.encoder(missing)
            self.cache.update({(query, text): vector for text, vector in zip(missing, values)})
        return [self.cache[query, text] for text in texts]

    def __call__(self, texts):
        return self.encode(texts)

    def encode_queries(self, texts):
        return self.encode(texts, True)


def aggregate(rows, key):
    metrics = [row[key] for row in rows]
    def average(name):
        values = [item[name] for item in metrics if item[name] is not None]
        return sum(values) / len(values) if values else None
    return {'cases': len(rows), **{name: average(name) for name in ('recall', 'hit', 'mrr', 'ndcg', 'false_positive')}}


def evaluate(args):
    started = time.perf_counter()
    raw = json.loads(args.cases.read_text(encoding='utf-8'))
    cases = raw['cases'] if isinstance(raw, dict) else raw
    encoder = CachedEncoder(LocalEmbedding(args.model_dir))
    thresholds = [.35, .4, .45, .5, .55, .6, .65, .7, .75, .8] if args.calibrate else [args.threshold]
    if args.calibrate and not any(case.get('split') == 'dev' for case in cases):
        raise ValueError('门槛只能用明确标记的开发集选择。')
    results = []
    with tempfile.TemporaryDirectory(prefix='bge-retrieval-eval-') as folder:
        store = MemoryStore(Path(folder) / 'eval.db')
        index = SemanticIndex(store, encoder, encoder.encoder.model_id, DIMENSIONS, provider='local-onnx')
        store.semantic_index = index
        for number, case in enumerate(cases):
            scope = f'eval-{number}'
            for doc in case['documents']:
                chunks = doc.get('chunks', [doc['content']])
                store.add_document(scope, 'eval-owner', doc['filename'], f'{scope}-{doc["filename"]}',
                                   '\n'.join(chunks), chunks)
            while index.update(scope)['remaining']:
                pass
            gold = set(case['expected_chunks'])
            row = {'id': case['id'], 'split': case.get('split', 'legacy'), 'category': case.get('category', 'legacy'),
                   'thresholds': {}, 'timings_ms': {}}
            for enabled, threshold in [(False, 0), *[(True, value) for value in thresholds]]:
                tick = time.perf_counter()
                index.minimum_similarity = threshold
                with patch.dict(os.environ, {'AI_VECTOR_ENABLED': 'true' if enabled else 'false'}):
                    with store._connect() as db:
                        found = store._select_document_chunks(db, scope, case['query'], allow_low_relevance=False, limit=5)
                ids = [f'{item["filename"]}:{item["chunk_index"]}' for item in found]
                label = str(threshold) if enabled else 'lexical'
                row['timings_ms'][label] = round((time.perf_counter() - tick) * 1000, 3)
                value = {'returned': ids, **retrieval_metrics(ids, gold)}
                if enabled:
                    row['thresholds'][label] = value
                else:
                    row['lexical'] = value
            results.append(row)
    choices = []
    if args.calibrate:
        dev = [row for row in results if row['split'] == 'dev']
        baseline = aggregate(dev, 'lexical')
        for threshold in thresholds:
            selected = [{**row, 'semantic': row['thresholds'][str(threshold)]} for row in dev]
            scores = aggregate(selected, 'semantic')
            choices.append({'threshold': threshold, **scores})
        safe = [value for value in choices if value['false_positive'] is not None
                and value['false_positive'] <= baseline['false_positive']]
        if not safe:
            raise ValueError('开发集没有不增加无答案误召回的门槛。')
        threshold = max(safe, key=lambda value: (value['recall'], value['mrr'], value['threshold']))['threshold']
    else:
        threshold = args.threshold
    for row in results:
        row['semantic'] = row['thresholds'][str(threshold)]
    groups = defaultdict(list)
    for row in results:
        groups[row['split']].append(row)
    by_split = {split: {'lexical': aggregate(rows, 'lexical'), 'hybrid': aggregate(rows, 'semantic')}
                for split, rows in groups.items()}
    overall = {'lexical': aggregate(results, 'lexical'), 'hybrid': aggregate(results, 'semantic')}
    left, right = overall['lexical'], overall['hybrid']
    # Preserve the existing synthetic metric gate; it is not a production-quality verdict.
    metric_gate = (len(results) >= 100 and right['recall'] - left['recall'] >= .10
                   and right['false_positive'] is not None and right['false_positive'] <= left['false_positive'])
    report = {'mode': 'local-real-embedding-synthetic-retrieval-not-answer-quality',
              'model': encoder.encoder.model_id, 'corpus_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest(),
              'threshold': threshold, 'calibrated_on': 'dev-only' if args.calibrate else 'fixed-before-run',
              'dev_threshold_comparison': choices, 'by_split': by_split, 'overall': overall,
              'existing_synthetic_metric_gate_passed': metric_gate,
              'production_gate_passed': None,
              'production_gate_status': 'not_assessed_real_documents_and_answer_faithfulness_missing',
              'elapsed_seconds': round(time.perf_counter() - started, 3),
              'new_paid_model_requests': 0, 'production_database_accessed': False,
              'source_hashes': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                  ('infrastructure/local_embedding.py', 'infrastructure/semantic_index.py', 'infrastructure/memory_store.py')},
              'model_manifest_sha256': hashlib.sha256((args.model_dir / 'manifest.json').read_bytes()).hexdigest(),
              'latency_note': 'Per-query timings include SQLite and cross-case encoder cache; not a production latency benchmark.',
              'details': results}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('threshold', 'by_split', 'production_gate_passed', 'elapsed_seconds')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--threshold', type=float, default=.65)
    parser.add_argument('--calibrate', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    evaluate(parser.parse_args())
