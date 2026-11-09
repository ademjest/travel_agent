"""Cross-domain retention SQL; filesystem cleanup stays with MaintenanceService."""
from __future__ import annotations
from datetime import datetime


class RetentionRepository:
    def __init__(self, store):
        self.store = store

    def purge_retained_data(
            self,
            *,
            chat_cutoff: datetime,
            event_cutoff: datetime,
            binding_cutoff: datetime,
            reservation_cutoff: datetime,
            image_cutoff: datetime,
            document_cutoff: datetime | None = None) -> dict[str, object]:
        counts: dict[str, object] = {}
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            counts['execution_diagnostics'] = connection.execute(
                'DELETE FROM execution_diagnostics WHERE julianday(created_at)<julianday(?)',
                (event_cutoff.isoformat(),)).rowcount
            counts['http_read_attempts'] = connection.execute(
                'DELETE FROM http_read_attempts WHERE julianday(created_at)<julianday(?)',
                (event_cutoff.isoformat(),)).rowcount
            counts["chat_messages"] = connection.execute(
                "DELETE FROM chat_messages WHERE julianday(created_at) < "
                "julianday(?)",
                (chat_cutoff.isoformat(),),
            ).rowcount
            counts["conversation_turns"] = connection.execute(
                "DELETE FROM conversation_turns WHERE julianday(created_at) < "
                "julianday(?)",
                (chat_cutoff.isoformat(),),
            ).rowcount
            counts["upload_bindings"] = connection.execute(
                """
                DELETE FROM upload_bindings
                WHERE (
                    consumed_at IS NOT NULL
                    AND julianday(consumed_at) < julianday(?)
                ) OR (
                    julianday(expires_at) < julianday(?)
                    AND julianday(created_at) < julianday(?)
                )
                """,
                (
                    binding_cutoff.isoformat(),
                    binding_cutoff.isoformat(),
                    binding_cutoff.isoformat(),
                ),
            ).rowcount
            counts["reservation_reminders"] = connection.execute(
                """
                DELETE FROM reservation_reminders
                WHERE status IN (
                    'sent', 'expired', 'blocked', 'failed', 'cancelled'
                )
                  AND julianday(
                        COALESCE(sent_at, queued_at, created_at)
                      ) < julianday(?)
                """,
                (reservation_cutoff.isoformat(),),
            ).rowcount
            counts["reservation_plans"] = connection.execute(
                """
                DELETE FROM reservation_plans
                WHERE status = 'cancelled'
                  AND julianday(updated_at) < julianday(?)
                """,
                (reservation_cutoff.isoformat(),),
            ).rowcount

            orphan_rows = connection.execute(
                """
                SELECT id, file_path
                FROM reservation_images
                WHERE julianday(updated_at) < julianday(?)
                  AND NOT EXISTS (
                    SELECT 1 FROM reservation_plans
                    WHERE reservation_plans.image_id = reservation_images.id
                  )
                """,
                (image_cutoff.isoformat(),),
            ).fetchall()
            if orphan_rows:
                connection.executemany(
                    "DELETE FROM reservation_images WHERE id = ?",
                    [(int(row["id"]),) for row in orphan_rows],
                )
            counts["reservation_images"] = len(orphan_rows)
            counts["image_paths"] = tuple(
                str(row["file_path"]) for row in orphan_rows
            )

            if document_cutoff is not None:
                counts["documents"] = connection.execute(
                    "DELETE FROM documents WHERE julianday(created_at) < "
                    "julianday(?)",
                    (document_cutoff.isoformat(),),
                ).rowcount
            else:
                counts["documents"] = 0

            counts["outbox_messages"] = connection.execute(
                """
                DELETE FROM outbox_messages
                WHERE status IN ('sent', 'dead_letter', 'cancelled')
                  AND julianday(COALESCE(sent_at, created_at)) < julianday(?)
                """,
                (event_cutoff.isoformat(),),
            ).rowcount
            counts["processed_events"] = connection.execute(
                """
                DELETE FROM processed_events
                WHERE status IN ('completed', 'dead_letter')
                  AND julianday(updated_at) < julianday(?)
                  AND NOT EXISTS (
                    SELECT 1 FROM outbox_messages
                    WHERE outbox_messages.event_id = processed_events.event_id
                  )
                """,
                (event_cutoff.isoformat(),),
            ).rowcount
        return counts

    def purge_auxiliary(self, cutoff):
        result = {}
        with self.store._connect() as connection:
            result['event_read_attempts'] = connection.execute('''DELETE FROM event_read_attempts
                WHERE event_id NOT IN (SELECT event_id FROM processed_events)''').rowcount
            result['wendao_requests']=connection.execute('DELETE FROM wendao_requests WHERE created_at < ?',(cutoff.isoformat(),)).rowcount
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():
                result['model_calls'] = connection.execute('DELETE FROM model_calls WHERE started_at < ?', (cutoff.isoformat(),)).rowcount
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_artifacts'").fetchone():
                result['tool_artifacts'] = connection.execute(
                    'DELETE FROM tool_artifacts WHERE created_at < ?', (cutoff.isoformat(),)).rowcount
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_summaries'").fetchone():
                result['conversation_summaries'] = connection.execute(
                    'DELETE FROM conversation_summaries WHERE updated_at < ?', (cutoff.isoformat(),)).rowcount
        return result
