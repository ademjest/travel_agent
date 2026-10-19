"""SQLite backup + migration rehearsal on copies only; no credentials/network/services."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway


def snapshot(db):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    counts = {name:db.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in tables}
    schema = [(row[0],row[1]) for row in db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
    return {'rows':counts,'schema_hash':hashlib.sha256(json.dumps(schema,sort_keys=True).encode()).hexdigest(),
            'integrity':db.execute('PRAGMA integrity_check').fetchone()[0]}


def backup(source,destination):
    if destination.exists():
        raise ValueError('备份目标已存在，拒绝覆盖。')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original:
        with sqlite3.connect(destination) as copied:
            original.backup(copied)
            return snapshot(copied)


def rehearse(source,directory):
    source = source.resolve()
    directory = directory.resolve()
    if not source.is_file() or directory.exists() or directory == source.parent:
        raise ValueError('需要存在的源数据库和全新的独立演练目录。')
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original = directory/'original-backup.db'
    working = directory/'migrated'/'travel_bot.db'
    before = backup(source,original)
    backup(original,working)
    store = MemoryStore(working)
    ModelGateway(store,SimpleNamespace())
    with store._connect() as db:
        first = snapshot(db)
    MemoryStore(working)
    ModelGateway(store,SimpleNamespace())
    with store._connect() as db:
        second = snapshot(db)
    # Restore rehearsal in a third isolated file, never into the source directory.
    restored = backup(original,directory/'restore-check.db')
    preserved = all(first['rows'].get(name)==count for name,count in before['rows'].items())
    source_unchanged = before_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    passed = (preserved and first==second and restored==before and source_unchanged
              and first['integrity']=='ok' and before['integrity']=='ok')
    return {'mode':'copy-only-migration-rehearsal','source':str(source),'directory':str(directory),
            'backup':str(original),'working_copy':str(working),'passed':passed,'row_counts_preserved':preserved,
            'idempotent':first==second,'restore_verified':restored==before,'source_file_unchanged':source_unchanged,
            'before':before,'after':first,'network_calls':0,'services_restarted':False,
            'generated_at':datetime.now(timezone.utc).isoformat(),
            'note':'副本包含正式数据，请按备份保护；报告只有计数和schema哈希，不含正文。原库未迁移。'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=rehearse(args.source,args.directory)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('passed','backup','source_file_unchanged','idempotent')},ensure_ascii=False))
    if not report['passed']:
        raise SystemExit(1)
