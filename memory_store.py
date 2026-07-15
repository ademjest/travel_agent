import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


RECENT_TURN_LIMIT = 6
MAX_DOCUMENT_CONTEXT_CHARS = 6000
MAX_DOCUMENT_CHUNKS = 3


@dataclass(frozen=True)
class ConversationTurn:
    user_content: str
    assistant_content: str
    created_at: str


@dataclass(frozen=True)
class StoredDocument:
    document_id: int
    filename: str
    is_new: bool


@dataclass(frozen=True)
class UploadBinding:
    binding_id: int
    group_openid: str
    issuer_openid: str
    c2c_user_openid: str
    expires_at: str


@dataclass(frozen=True)
class UploadBindingRedemption:
    status: str
    binding: UploadBinding | None = None


class MemoryStore:
    def __init__(self, database_path: str | Path | None = None):
        default_path = Path(__file__).resolve().parent / "data" / "travel_bot.db"
        self.database_path = Path(database_path or default_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_openid TEXT NOT NULL,
                    member_openid TEXT NOT NULL,
                    user_msg_id TEXT NOT NULL UNIQUE,
                    user_content TEXT NOT NULL,
                    assistant_content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_turns_session
                ON conversation_turns(group_openid, member_openid, id DESC);

                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_openid TEXT NOT NULL,
                    uploader_openid TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    text_length INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_openid, sha256)
                );

                CREATE INDEX IF NOT EXISTS idx_documents_group
                ON documents(group_openid, id DESC);

                CREATE TABLE IF NOT EXISTS document_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                        ON DELETE CASCADE,
                    UNIQUE(document_id, chunk_index)
                );

                CREATE TABLE IF NOT EXISTS upload_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code_hash TEXT NOT NULL UNIQUE,
                    group_openid TEXT NOT NULL,
                    issuer_openid TEXT NOT NULL,
                    c2c_user_openid TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    redeemed_at TEXT,
                    consumed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_upload_bindings_private_user
                ON upload_bindings(c2c_user_openid, id DESC);

                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(documents)"
                ).fetchall()
            }
            if "summary" not in columns:
                connection.execute(
                    "ALTER TABLE documents "
                    "ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
                )

    def claim_event(
            self,
            event_id: str,
            now: datetime | None = None) -> bool:
        created_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO processed_events (event_id, created_at)
                VALUES (?, ?)
                """,
                (event_id, created_at.isoformat()),
            )
        return cursor.rowcount == 1

    def create_upload_binding(
            self,
            code_hash: str,
            group_openid: str,
            issuer_openid: str,
            expires_at: datetime,
            now: datetime | None = None) -> int:
        created_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO upload_bindings (
                    code_hash,
                    group_openid,
                    issuer_openid,
                    created_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    code_hash,
                    group_openid,
                    issuer_openid,
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    def redeem_upload_binding(
            self,
            code_hash: str,
            c2c_user_openid: str,
            now: datetime | None = None) -> UploadBindingRedemption:
        redeemed_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE upload_bindings
                SET c2c_user_openid = ?, redeemed_at = ?
                WHERE code_hash = ?
                  AND redeemed_at IS NULL
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (
                    c2c_user_openid,
                    redeemed_at.isoformat(),
                    code_hash,
                    redeemed_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM upload_bindings WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if cursor.rowcount != 1:
                if row is None:
                    return UploadBindingRedemption(status="invalid")
                if datetime.fromisoformat(row["expires_at"]) <= redeemed_at:
                    return UploadBindingRedemption(status="expired")
                if row["consumed_at"]:
                    return UploadBindingRedemption(status="used")
                if row["c2c_user_openid"] != c2c_user_openid:
                    return UploadBindingRedemption(status="used")
                return UploadBindingRedemption(
                    status="already_redeemed",
                    binding=self._upload_binding_from_row(row),
                )

            connection.execute(
                """
                UPDATE upload_bindings
                SET consumed_at = ?
                WHERE c2c_user_openid = ?
                  AND redeemed_at IS NOT NULL
                  AND consumed_at IS NULL
                  AND id != ?
                """,
                (redeemed_at.isoformat(), c2c_user_openid, row["id"]),
            )

        return UploadBindingRedemption(
            status="redeemed",
            binding=self._upload_binding_from_row(row),
        )

    def get_pending_upload_binding(
            self,
            c2c_user_openid: str,
            now: datetime | None = None) -> UploadBinding | None:
        current_time = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM upload_bindings
                WHERE c2c_user_openid = ?
                  AND redeemed_at IS NOT NULL
                  AND consumed_at IS NULL
                  AND expires_at > ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (c2c_user_openid, current_time.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return self._upload_binding_from_row(row)

    def claim_pending_upload_binding(
            self,
            c2c_user_openid: str,
            now: datetime | None = None) -> UploadBinding | None:
        claimed_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM upload_bindings
                WHERE c2c_user_openid = ?
                  AND redeemed_at IS NOT NULL
                  AND consumed_at IS NULL
                  AND expires_at > ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (c2c_user_openid, claimed_at.isoformat()),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE upload_bindings
                SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (claimed_at.isoformat(), row["id"]),
            )
            if cursor.rowcount != 1:
                return None
        return self._upload_binding_from_row(row)

    def consume_upload_binding(
            self,
            binding_id: int,
            now: datetime | None = None) -> None:
        consumed_at = now or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE upload_bindings
                SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (consumed_at.isoformat(), binding_id),
            )

    @staticmethod
    def _upload_binding_from_row(row: sqlite3.Row) -> UploadBinding:
        return UploadBinding(
            binding_id=int(row["id"]),
            group_openid=row["group_openid"],
            issuer_openid=row["issuer_openid"],
            c2c_user_openid=row["c2c_user_openid"] or "",
            expires_at=row["expires_at"],
        )

    def has_message(self, user_msg_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_turns WHERE user_msg_id = ?",
                (user_msg_id,),
            ).fetchone()
        return row is not None

    def save_turn(
            self,
            group_openid: str,
            member_openid: str,
            user_msg_id: str,
            user_content: str,
            assistant_content: str) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_turns (
                    group_openid,
                    member_openid,
                    user_msg_id,
                    user_content,
                    assistant_content,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_openid,
                    member_openid,
                    user_msg_id,
                    user_content,
                    assistant_content,
                    created_at,
                ),
            )

    def get_recent_turns(
            self,
            group_openid: str,
            member_openid: str,
            limit: int = RECENT_TURN_LIMIT) -> tuple[ConversationTurn, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_content, assistant_content, created_at
                FROM conversation_turns
                WHERE group_openid = ? AND member_openid = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_openid, member_openid, limit),
            ).fetchall()

        return tuple(
            ConversationTurn(
                user_content=row["user_content"],
                assistant_content=row["assistant_content"],
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        )

    def add_document(
            self,
            group_openid: str,
            uploader_openid: str,
            filename: str,
            sha256: str,
            full_text: str,
            chunks: list[str],
            summary: str = "") -> StoredDocument:
        created_at = datetime.now(timezone.utc).isoformat()
        preview = full_text[:500]

        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, filename FROM documents
                WHERE group_openid = ? AND sha256 = ?
                """,
                (group_openid, sha256),
            ).fetchone()
            if existing:
                return StoredDocument(
                    document_id=existing["id"],
                    filename=existing["filename"],
                    is_new=False,
                )

            cursor = connection.execute(
                """
                INSERT INTO documents (
                    group_openid,
                    uploader_openid,
                    filename,
                    sha256,
                    preview,
                    summary,
                    text_length,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_openid,
                    uploader_openid,
                    filename,
                    sha256,
                    preview,
                    summary,
                    len(full_text),
                    created_at,
                ),
            )
            document_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO document_chunks (
                    document_id, chunk_index, content
                ) VALUES (?, ?, ?)
                """,
                [
                    (document_id, index, chunk)
                    for index, chunk in enumerate(chunks)
                ],
            )

        return StoredDocument(
            document_id=document_id,
            filename=filename,
            is_new=True,
        )

    def build_document_context(
            self,
            group_openid: str,
            query: str,
            max_chars: int = MAX_DOCUMENT_CONTEXT_CHARS) -> str:
        with self._connect() as connection:
            documents = connection.execute(
                """
                SELECT id, filename, preview, summary, created_at
                FROM documents
                WHERE group_openid = ?
                ORDER BY id DESC
                LIMIT 10
                """,
                (group_openid,),
            ).fetchall()
            chunks = connection.execute(
                """
                SELECT
                    c.document_id,
                    c.chunk_index,
                    c.content,
                    d.filename,
                    d.id AS document_order
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.group_openid = ?
                ORDER BY d.id DESC, c.chunk_index ASC
                """,
                (group_openid,),
            ).fetchall()

        if not documents:
            return ""

        terms = self._search_terms(query)
        ranked = []
        for row in chunks:
            content = row["content"]
            score = sum(
                content.lower().count(term) * max(1, len(term))
                for term in terms
            )
            ranked.append((score, row["document_order"], -row["chunk_index"], row))

        ranked.sort(reverse=True, key=lambda item: item[:3])
        selected = [
            item[3]
            for item in ranked
            if item[0] > 0
        ][:MAX_DOCUMENT_CHUNKS]

        if not selected and chunks:
            newest_document_id = documents[0]["id"]
            selected = [
                row for row in chunks
                if row["document_id"] == newest_document_id
            ][:1]

        lines = ["群内已保存的旅行文档："]
        for document in documents:
            overview = document["summary"] or document["preview"]
            overview = re.sub(r"\s+", " ", overview).strip()[:800]
            lines.append(f"- {document['filename']}：{overview}")

        if selected:
            lines.append("与当前问题相关的文档片段：")
            for row in selected:
                lines.append(
                    f"[{row['filename']} / 片段 {row['chunk_index'] + 1}]\n"
                    f"{row['content']}"
                )

        context = "\n".join(lines)
        return context[:max_chars]

    @staticmethod
    def _search_terms(text: str) -> set[str]:
        normalized = text.lower()
        terms = set(re.findall(r"[a-z0-9]{2,}", normalized))
        for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized):
            if 2 <= len(sequence) <= 8:
                terms.add(sequence)
            terms.update(
                sequence[index:index + 2]
                for index in range(len(sequence) - 1)
            )

        stop_terms = {
            "什么", "怎么", "如何", "一下", "这个", "那个",
            "帮我", "看看", "可以", "我们", "你们",
        }
        return terms - stop_terms
