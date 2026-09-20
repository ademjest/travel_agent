import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.audit_legacy_state import audit


class LegacyAuditTests(unittest.TestCase):
    def test_only_proven_matching_sent_reply_is_repaired_on_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'old.db', Path(folder)/'copy.db'
            with closing(sqlite3.connect(source)) as db, db:
                db.execute('CREATE TABLE conversation_turns (id INTEGER, group_openid TEXT, member_openid TEXT, user_msg_id TEXT, assistant_content TEXT)')
                db.executemany('INSERT INTO conversation_turns VALUES (?,?,?,?,?)', [
                    (1, 'onebot:g', 'u', 'ok', ''), (2, 'onebot:g', 'u', 'wrong-owner', ''),
                    (3, 'onebot:g', 'u', 'no-source', ''), (4, 'onebot:g', 'u', 'keep', 'original')])
                db.execute('CREATE TABLE outbox_messages (event_id TEXT, status TEXT, channel TEXT, platform TEXT, target_id TEXT, sender_id TEXT, payload_json TEXT)')
                db.executemany('INSERT INTO outbox_messages VALUES (?,?,?,?,?,?,?)', [
                    ('ok', 'sent', 'group', 'onebot', 'g', 'u', json.dumps({'message': '请告诉我城市'})),
                    ('wrong-owner', 'sent', 'group', 'onebot', 'g', 'other', json.dumps({'message': 'wrong'}))])
                db.execute('CREATE TABLE reservation_reminders (id INTEGER, scheduled_at_utc TEXT, status TEXT)')
                db.execute("INSERT INTO reservation_reminders VALUES (7, '2030-09-10T02:00:00+00:00', 'sent')")
            before = source.read_bytes()
            report = audit(source, target)
            self.assertEqual(report['proven_repair_candidates'], 1)
            self.assertEqual(report['unresolved'], 2)
            self.assertEqual(source.read_bytes(), before)
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('SELECT assistant_content FROM conversation_turns WHERE id=1').fetchone()[0], '请告诉我城市')
                self.assertEqual(db.execute('SELECT * FROM reservation_reminders').fetchone(), (7, '2030-09-10T02:00:00+00:00', 'sent'))
            with self.assertRaises(ValueError): audit(source, source)
            with self.assertRaises(ValueError): audit(source, target)
