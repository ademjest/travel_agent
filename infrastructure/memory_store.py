from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from core.data_paths import database_path as default_database_path
from infrastructure.schema import initialize
from infrastructure.delivery_repository import DeliveryRepository
from infrastructure.document_repository import DocumentRepository
from infrastructure.conversation_repository import ConversationRepository
from infrastructure.upload_repository import UploadRepository
from infrastructure.reservation_repository import ReservationRepository
from infrastructure.retention_repository import RetentionRepository


class MemoryStore:
    def __init__(self, database_path: str | Path | None = None):
        self.database_path = Path(database_path or default_database_path())
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._document_fts_available = False
        initialize(self)
        self.delivery = DeliveryRepository(self)
        self.retention = RetentionRepository(self)
        self.reservations = ReservationRepository(self)
        self.documents = DocumentRepository(self)
        self.conversations = ConversationRepository(self)
        self.uploads = UploadRepository(self)
        from infrastructure.task_repository import TaskRepository
        self.tasks = TaskRepository(self)
        from infrastructure.reminder_repository import ReminderRepository
        self.reminders = ReminderRepository(self)
        from infrastructure.policy_repository import PolicyRepository
        self.policies = PolicyRepository(self)
        from infrastructure.trip_repository import TripRepository
        self.trips = TripRepository(self)
        from infrastructure.inbox_repository import InboxRepository
        self.inbox = InboxRepository(self)
        from infrastructure.media_repository import MediaRepository
        self.media = MediaRepository(self)
        from infrastructure.scheduled_query_repository import ScheduledQueryRepository
        self.scheduled_queries = ScheduledQueryRepository(self)
        from infrastructure.policy_watch_repository import PolicyWatchRepository
        self.policy_watches = PolicyWatchRepository(self)
        from infrastructure.preference_repository import PreferenceRepository
        self.preferences = PreferenceRepository(self)
        from infrastructure.research_repository import ResearchRepository
        self.research = ResearchRepository(self)
        from infrastructure.planning_repository import PlanningRepository
        self.planning = PlanningRepository(self)
        from infrastructure.tool_artifact_repository import ToolArtifactRepository
        self.tool_artifacts = ToolArtifactRepository(self)
        from infrastructure.conversation_summary_repository import ConversationSummaryRepository
        self.conversation_summaries = ConversationSummaryRepository(self)
        from infrastructure.execution_diagnostics import ExecutionDiagnostics
        self.diagnostics = ExecutionDiagnostics(self)

    @contextmanager
    def _connect(self, timeout=10):
        connection = sqlite3.connect(self.database_path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            from core.execution_scope import connection_for_scope
            scoped_connection = connection_for_scope(connection, self.database_path)
            yield scoped_connection
            scoped_connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()










    _payload_reply_text = staticmethod(DeliveryRepository._payload_reply_text)




    _outbox_message = staticmethod(DeliveryRepository._outbox_message)
