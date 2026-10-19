"""Compare FTS/keyword retrieval with the optional local hybrid index."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.memory_store import MemoryStore
from core.retrieval_metrics import retrieval_metrics


def retrieve(store, query, *, hybrid):
    with patch.dict(os.environ, {'AI_VECTOR_ENABLED': 'true' if hybrid else 'false'}):
        with store._connect() as db:
            rows = store._select_document_chunks(db, 'eval', query, allow_low_relevance=False,limit=5)
    return [f"{row['filename']}:{row['chunk_index']}" for row in rows]


def evaluate(cases):
    lexical_hits = hybrid_hits = 0
    details = []
    for index, case in enumerate(cases, 1):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder) / 'retrieval.db')
            for doc_index, document in enumerate(case['documents']):
                text = document['content']
                store.add_document('eval', 'owner', document['filename'],
                                   f'case-{index}-doc-{doc_index}', text[:500], [text])
            lexical = retrieve(store, case['query'], hybrid=False)
            hybrid = retrieve(store, case['query'], hybrid=True)
        expected = set(case.get('expected_chunks', [name+':0' for name in case.get('expected_filenames',[])]))
        lexical_ok = bool(expected.intersection(lexical))
        hybrid_ok = bool(expected.intersection(hybrid))
        lexical_hits += lexical_ok
        hybrid_hits += hybrid_ok
        details.append({'id': case.get('id', index), 'category':case.get('category','legacy'),
                        'lexical': lexical, 'hybrid': hybrid, 'lexical_hit': lexical_ok, 'hybrid_hit': hybrid_ok,
                        'lexical_metrics':retrieval_metrics(lexical,expected),'hybrid_metrics':retrieval_metrics(hybrid,expected)})
    total = len(cases)
    def average(key,metric):
        values=[row[key][metric] for row in details if row[key][metric] is not None]
        return sum(values)/len(values) if values else None
    lexical_recall=average('lexical_metrics','recall')
    hybrid_recall=average('hybrid_metrics','recall')
    lexical_false=average('lexical_metrics','false_positive')
    hybrid_false=average('hybrid_metrics','false_positive')
    improvement=(hybrid_recall-lexical_recall)*100 if lexical_recall is not None and hybrid_recall is not None else 0
    return {'metric_version':'chunk-v3', 'retrieval':'fts-vs-lexical-hash-rrf',
            'cases': total, 'lexical_recall_at_5': lexical_recall,
            'hybrid_recall_at_5': hybrid_recall, 'lexical_hit_at_5':average('lexical_metrics','hit'),
            'hybrid_hit_at_5':average('hybrid_metrics','hit'),
            'lexical_false_positive_rate':lexical_false,'hybrid_false_positive_rate':hybrid_false,
            'improvement_points': improvement,
            'go': bool(total>=100 and improvement>=10 and hybrid_false is not None and lexical_false is not None and hybrid_false<=lexical_false),
            'details': details}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, default=Path('evals/retrieval_cases_v2.json'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    report = evaluate(cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('cases','lexical_recall_at_5','hybrid_recall_at_5','go')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
