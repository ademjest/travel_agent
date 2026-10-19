"""Export a privacy-safe report using a strictly read-only SQLite connection."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.model_gateway import usage_report


class ReadOnlyStore:
    def __init__(self, path):
        self.database_path = Path(path).resolve()
        if not self.database_path.is_file():
            raise FileNotFoundError('数据库不存在；只读报表不会创建数据库。')

    @contextmanager
    def _connect(self):
        uri = self.database_path.as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        try:
            yield db
        finally:
            db.close()


def build_report(database, cases=None):
    store = ReadOnlyStore(database)
    names = ('model_calls','model_usage_periods','model_usage_ledger','conversation_summaries',
             'document_chunk_vectors','tool_artifacts','planning_artifacts','research_contexts')
    with store._connect() as db:
        features={name:bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()) for name in names}
        counts={name:db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] if available else 0 for name,available in features.items()}
    case_count=0
    if cases and Path(cases).exists():
        value=json.loads(Path(cases).read_text(encoding='utf-8'))
        case_count=len(value if isinstance(value,list) else value.get('cases',[]))
    return {'report_version':'v3-readonly','generated_at':datetime.now(timezone.utc).isoformat(),
            'case_count':case_count,'usage':usage_report(store),'features':features,'counts':counts,
            'notes':['纯只读，未创建表或执行迁移。','不包含聊天正文、凭据或事件标识；没有记录不代表零消费。']}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',required=True)
    parser.add_argument('--cases',type=Path,default=ROOT/'evals/task_cases_v1.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=build_report(args.database,args.cases)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(args.output),'calls':report['usage']['totals']['calls']}))
