"""Audit legacy SQLite data read-only; optionally repair proven empty replies on a NEW copy.

Original reminders remain in their existing scheduler. This does not create tasks,
convert reminder IDs, replay messages, or change the source database.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.chat_transport import storage_scope_id
from infrastructure.memory_store import MemoryStore


def audit(database, repaired_copy=None):
    database = Path(database).resolve(strict=True)
    if repaired_copy:
        repaired_copy = Path(repaired_copy).resolve()
        if repaired_copy == database or repaired_copy.exists():
            raise ValueError('Repair destination must be a new file, distinct from the original database.')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute('BEGIN')
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed; no repair attempted.')
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        patches = []
        empty = 0
        if {'conversation_turns', 'outbox_messages'} <= tables:
            rows = source.execute("SELECT * FROM conversation_turns WHERE trim(assistant_content)=''").fetchall()
            empty = len(rows)
            for row in rows:
                matches = source.execute("SELECT * FROM outbox_messages WHERE event_id=? AND status='sent' AND channel='group'", (row['user_msg_id'],)).fetchall()
                if len(matches) != 1:
                    continue
                outbox = matches[0]
                if (storage_scope_id(outbox['platform'], outbox['target_id']) != row['group_openid']
                        or outbox['sender_id'] != row['member_openid']):
                    continue
                try:
                    text = MemoryStore._payload_reply_text(json.loads(outbox['payload_json']))
                except (ValueError, TypeError, AttributeError):
                    continue
                if text.strip():
                    patches.append((text, row['id']))
        legacy = dict(source.execute('SELECT status, count(*) FROM reservation_reminders GROUP BY status')) if 'reservation_reminders' in tables else {}
        report = dict(empty_replies=empty, proven_repair_candidates=len(patches), unresolved=empty-len(patches),
            legacy_reminders_by_status=legacy, original_modified=False, repaired_copy=str(repaired_copy or ''),
            reminder_strategy='retain existing IDs, UTC times, delivery states and legacy scheduler; no duplicate personal reminders')
        if repaired_copy:
            repaired_copy.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation avoids replacing a destination that appears between checks.
            with repaired_copy.open('xb'):
                pass
            with closing(sqlite3.connect(repaired_copy)) as target, target:
                source.backup(target)
                target.executemany("UPDATE conversation_turns SET assistant_content=? WHERE id=? AND trim(assistant_content)=''", patches)
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Repaired copy failed integrity check; do not use it.')
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--repaired-copy', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.resolve() in {args.database.resolve(), args.repaired_copy.resolve() if args.repaired_copy else None}:
        parser.error('Report path must differ from source and repair destination.')
    result = audit(args.database, args.repaired_copy)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
