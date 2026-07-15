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
