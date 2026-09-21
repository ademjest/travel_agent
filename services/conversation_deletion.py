"""Revoke a Web conversation atomically, then purge its records and owned files."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json


def now_text():
    return datetime.now(timezone.utc).isoformat()


class ConversationDeletionService:
    def __init__(self, repository):
        self.repository = repository
        self.store = repository.store
        self.lifecycle = repository.lifecycle
        self.wakeup = asyncio.Event()

    @staticmethod
    def _table_exists(db, name):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def status(self, identity):
        with self.store._connect() as db:
            row = db.execute('SELECT d.conversation_id,d.state,d.requested_at,d.updated_at,d.error,c.title FROM web_conversation_deletions d LEFT JOIN web_conversations c ON c.id=d.conversation_id WHERE d.conversation_id=?', (identity,)).fetchone()
        if row is None:
            raise LookupError('删除作业不存在。')
        return dict(row)

    def list_jobs(self):
        with self.store._connect() as db:
            return [dict(row) for row in db.execute("SELECT d.conversation_id,d.state,d.requested_at,d.updated_at,d.error,c.title FROM web_conversation_deletions d LEFT JOIN web_conversations c ON c.id=d.conversation_id WHERE d.state!='completed' ORDER BY d.requested_at")]

    def summary(self, identity):
        with self.store._connect() as db:
            db.execute('BEGIN')
            conversation = self.lifecycle.check(identity, db)
            scope = 'web:' + identity
            def count(query, *args):
                return db.execute(query, args).fetchone()[0]
            return {'id': identity, 'title': conversation['title'],
                'messages': count('SELECT count(*) FROM web_requests WHERE conversation_id=?', identity)
                    + count("SELECT count(*) FROM web_deliveries WHERE conversation_id=? AND kind!='progress'", identity),
                'attachments': count('SELECT count(*) FROM web_uploads WHERE conversation_id=?', identity),
                'active_tasks': count("SELECT count(*) FROM inbox_jobs WHERE platform='web' AND scope_id=? AND status IN ('pending','running','retry')", identity),
                'reminders': count("SELECT count(*) FROM personal_reminders r JOIN reminder_occurrences o ON o.reminder_id=r.reminder_id AND o.version=r.version WHERE r.platform='web' AND r.group_id=? AND r.status='active' AND o.status IN ('pending','queued','failed')", identity)
                    + count("SELECT count(*) FROM reservation_reminders WHERE platform='web' AND group_id=? AND status IN ('pending','queued')", identity),
                'trips': count("SELECT count(*) FROM trips WHERE platform='web' AND scope_id=?", scope),
                'scheduled_queries': count("SELECT count(*) FROM scheduled_queries WHERE platform='web' AND group_id=? AND status IN ('active','queued')", identity),
                'policy_watches': count("SELECT count(*) FROM policy_watches WHERE platform='web' AND group_id=? AND status IN ('active','queued')", identity)}

    def _collect(self, db, identity):
        scope = 'web:' + identity
        events, paths = set(), set()
        queries = [
            ("SELECT event_key FROM inbox_jobs WHERE platform='web' AND scope_id=?", (identity,)),
            ("SELECT event_id FROM outbox_messages WHERE platform='web' AND target_id=?", (identity,)),
            ("SELECT source_event_id,last_event_id FROM travel_tasks WHERE platform='web' AND scope_id=?", (scope,)),
            ("SELECT e.event_id FROM task_event_results e JOIN travel_tasks t ON t.task_id=e.task_id WHERE t.platform='web' AND t.scope_id=?", (scope,)),
            ("SELECT source_event_id FROM trips WHERE platform='web' AND scope_id=?", (scope,)),
            ("SELECT v.event_id FROM trip_versions v JOIN trips t ON t.trip_id=v.trip_id WHERE t.platform='web' AND t.scope_id=?", (scope,)),
            ("SELECT source_event_id FROM personal_reminders WHERE platform='web' AND group_id=?", (identity,)),
            ("SELECT o.outbox_event_id FROM reminder_occurrences o JOIN personal_reminders r ON r.reminder_id=o.reminder_id WHERE r.platform='web' AND r.group_id=?", (identity,)),
            ("SELECT source_event_id FROM reservation_plans WHERE platform='web' AND group_id=?", (identity,)),
            ("SELECT outbox_event_id FROM reservation_reminders WHERE platform='web' AND group_id=?", (identity,)),
            ("SELECT source_event_id,output_event_id FROM scheduled_queries WHERE platform='web' AND group_id=?", (identity,)),
            ("SELECT source_event_id,last_notice_event FROM policy_watches WHERE platform='web' AND group_id=?", (identity,)),
            ("SELECT event_key FROM media_observations WHERE platform='web' AND scope_id=?", (scope,)),
            ("SELECT event_id FROM outbound_message_links WHERE platform='web' AND scope_id=?", (scope,)),
        ]
        for query, args in queries:
            for row in db.execute(query, args):
                events.update(value for value in row if value)
        prefixes = [f'web:group:{identity}:', f'inbox-notice:web:group:{identity}:']
        prefixes += [f'policy-watch:{row[0]}:' for row in db.execute("SELECT watch_id FROM policy_watches WHERE platform='web' AND group_id=?", (identity,))]
        for prefix in prefixes:
            events.update(row[0] for row in db.execute('SELECT event_id FROM processed_events WHERE substr(event_id,1,?)=?', (len(prefix), prefix)))
        for event in tuple(events):
            events.add('inbox-notice:' + event)
        for query, args in [
            ('SELECT relative_path FROM web_uploads WHERE conversation_id=?', (identity,)),
            ("SELECT a.file_path FROM inbox_assets a JOIN inbox_jobs j ON j.event_key=a.event_key WHERE j.platform='web' AND j.scope_id=?", (identity,)),
            ("SELECT file_path FROM reservation_images WHERE platform='web' AND group_id=?", (identity,)),
            ("SELECT file_path FROM media_observations WHERE platform='web' AND scope_id=?", (scope,)),
            ('SELECT path FROM web_file_intents WHERE conversation_id=?', (identity,)),
        ]:
            paths.update(row[0] for row in db.execute(query, args) if row[0])
        # Scope-specific legacy media directories may include unregistered .part files from an interrupted write.
        owners = {row[0] for row in db.execute("SELECT owner_id FROM media_observations WHERE platform='web' AND scope_id=?", (scope,))}
        owners.add('local-owner')
        for owner in owners:
            name = hashlib.sha256(f'web:{scope}:{owner}'.encode()).hexdigest()[:24]
            folder = self.lifecycle.root / 'media' / name
            if folder.exists() and not folder.is_symlink() and folder.resolve().is_relative_to(self.lifecycle.root):
                paths.update(str(path) for path in folder.iterdir() if path.is_file())
        return sorted(events), sorted(paths)

    def request(self, identity):
        with self.lifecycle.file_lock, self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT 1 FROM web_conversation_deletions WHERE conversation_id=?', (identity,)).fetchone()
            if old:
                return self.status(identity)
            self.lifecycle.check(identity, db)
            events, files = self._collect(db, identity)
            now = now_text()
            db.execute('INSERT INTO web_conversation_deletions VALUES (?,?,?,?,?,?,?)',
                       (identity, 'pending', json.dumps(files), json.dumps(events), now, now, ''))
            db.execute("UPDATE inbox_jobs SET status='cancelled',claim_token=NULL,lease_until=NULL,capture_token=NULL,capture_lease_until=NULL WHERE platform='web' AND scope_id=? AND status IN ('pending','running','retry')", (identity,))
            db.execute("UPDATE personal_reminders SET status='cancelled' WHERE platform='web' AND group_id=?", (identity,))
            db.execute("UPDATE reminder_occurrences SET status='cancelled' WHERE reminder_id IN (SELECT reminder_id FROM personal_reminders WHERE platform='web' AND group_id=?)", (identity,))
            for table in ('reservation_reminders', 'scheduled_queries', 'policy_watches'):
                db.execute(f"UPDATE {table} SET status='cancelled' WHERE platform='web' AND group_id=?", (identity,))
            db.execute("UPDATE outbox_messages SET status='cancelled',claim_token=NULL,lease_expires_at=NULL WHERE platform='web' AND target_id=? AND status!='sent'", (identity,))
            db.executemany("UPDATE processed_events SET status='completed',claim_token=NULL,lease_expires_at=NULL WHERE event_id=?", [(key,) for key in events])
        return self.status(identity)

    def _purge_records(self, db, identity, events):
        scope = 'web:' + identity
        db.execute("DELETE FROM research_runs WHERE platform='web' AND scope=?", (identity,))
        db.execute("DELETE FROM preference_candidates WHERE platform='web' AND conversation=?", (identity,))
        db.execute('CREATE TEMP TABLE deletion_events (id TEXT PRIMARY KEY)')
        db.executemany('INSERT OR IGNORE INTO deletion_events VALUES (?)', [(key,) for key in events])
        db.execute("UPDATE user_preferences SET source_event='' WHERE platform='web' AND source_event IN (SELECT id FROM deletion_events)")
        if self._table_exists(db, 'model_calls'):
            db.execute("DELETE FROM model_calls WHERE event_key IN (SELECT id FROM deletion_events) OR job_id IN (SELECT id FROM inbox_jobs WHERE platform='web' AND scope_id=?)", (identity,))
        db.execute('DELETE FROM web_requests WHERE conversation_id=?', (identity,))
        db.execute('DELETE FROM web_deliveries WHERE conversation_id=?', (identity,))
        db.execute('DELETE FROM web_uploads WHERE conversation_id=?', (identity,))
        db.execute('DELETE FROM web_file_intents WHERE conversation_id=?', (identity,))
        db.execute("DELETE FROM inbox_jobs WHERE platform='web' AND scope_id=?", (identity,))
        db.execute('DELETE FROM outbox_messages WHERE event_id IN (SELECT id FROM deletion_events)')
        db.execute('DELETE FROM event_tool_results WHERE event_id IN (SELECT id FROM deletion_events)')
        db.execute("DELETE FROM outbound_message_links WHERE (platform='web' AND scope_id=?) OR event_id IN (SELECT id FROM deletion_events)", (scope,))
        db.execute('DELETE FROM task_event_results WHERE event_id IN (SELECT id FROM deletion_events)')
        db.execute("DELETE FROM trip_versions WHERE trip_id IN (SELECT trip_id FROM trips WHERE platform='web' AND scope_id=?)", (scope,))
        db.execute("DELETE FROM trips WHERE platform='web' AND scope_id=?", (scope,))
        db.execute("DELETE FROM reminder_occurrences WHERE reminder_id IN (SELECT reminder_id FROM personal_reminders WHERE platform='web' AND group_id=?)", (identity,))
        db.execute("DELETE FROM personal_reminders WHERE platform='web' AND group_id=?", (identity,))
        for table in ('reservation_plans', 'reservation_images', 'reservation_workflows', 'scheduled_queries', 'policy_watches'):
            db.execute(f"DELETE FROM {table} WHERE platform='web' AND group_id=?", (identity,))
        db.execute("DELETE FROM media_observations WHERE platform='web' AND scope_id=?", (scope,))
        db.execute("DELETE FROM travel_tasks WHERE platform='web' AND scope_id=?", (scope,))
        db.execute('DELETE FROM document_chunks WHERE document_id IN (SELECT id FROM documents WHERE group_openid=?)', (scope,))
        db.execute('DELETE FROM documents WHERE group_openid=?', (scope,))
        db.execute('DELETE FROM conversation_turns WHERE group_openid=?', (scope,))
        db.execute("DELETE FROM chat_messages WHERE platform='web' AND group_id=?", (identity,))
        db.execute('DELETE FROM upload_bindings WHERE group_openid=?', (scope,))
        db.execute('DELETE FROM processed_events WHERE event_id IN (SELECT id FROM deletion_events)')

    def cleanup(self, identity):
        with self.lifecycle.file_lock:
            try:
                with self.store._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT * FROM web_conversation_deletions WHERE conversation_id=?', (identity,)).fetchone()
                    if row is None:
                        raise LookupError('删除作业不存在。')
                    if row['state'] == 'completed':
                        return self.status(identity)
                    events, files = json.loads(row['events_json']), json.loads(row['files_json'])
                    self._purge_records(db, identity, events)
                    db.execute("UPDATE web_conversation_deletions SET state='cleaning',updated_at=?,error='' WHERE conversation_id=?", (now_text(), identity))
                failed = []
                for path in files:
                    try:
                        self.lifecycle.remove_unreferenced(path)
                    except (OSError, ValueError):
                        failed.append(path)
                with self.store._connect() as db:
                    if failed:
                        db.execute("UPDATE web_conversation_deletions SET state='failed',files_json=?,events_json='[]',updated_at=?,error=? WHERE conversation_id=?",
                                   (json.dumps(failed), now_text(), '部分附件未清理：文件可能被占用，或路径不符合安全范围。可稍后重试。', identity))
                    else:
                        db.execute('DELETE FROM web_conversations WHERE id=?', (identity,))
                        db.execute("UPDATE web_conversation_deletions SET state='completed',files_json='[]',events_json='[]',updated_at=?,error='' WHERE conversation_id=?", (now_text(), identity))
            except Exception as error:
                with self.store._connect() as db:
                    db.execute("UPDATE web_conversation_deletions SET state='failed',updated_at=?,error=? WHERE conversation_id=?",
                               (now_text(), '清理尚未完成（'+type(error).__name__+'），会话保持停用，可重试。', identity))
        return self.status(identity)

    def retry(self, identity):
        job = self.status(identity)
        if job['state'] == 'failed':
            with self.store._connect() as db:
                db.execute("UPDATE web_conversation_deletions SET state='pending',error='',updated_at=? WHERE conversation_id=? AND state='failed'", (now_text(), identity))
        return self.status(identity)

    async def run(self):
        while True:
            # Failed jobs remain visible and require an explicit retry; pending/cleaning resume after restart.
            for job in await asyncio.to_thread(self.list_jobs):
                if job['state'] in {'pending', 'cleaning'}:
                    await asyncio.to_thread(self.cleanup, job['conversation_id'])
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
