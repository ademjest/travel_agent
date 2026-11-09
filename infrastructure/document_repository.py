"""DocumentRepository: scoped storage, no business routing."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from core.model_policy import current_policy
from core.retrieval_metrics import reciprocal_rank_fusion
from infrastructure.local_vector_index import vector_candidates

MAX_DOCUMENT_CONTEXT_CHARS = 3200
MAX_DOCUMENT_CHUNKS = 2
MAX_DOCUMENT_CANDIDATES = 10
MAX_FTS_QUERY_TERMS = 24
MAX_FALLBACK_QUERY_TERMS = 12


@dataclass(frozen=True)
class StoredDocument:
    document_id: int
    filename: str
    is_new: bool


@dataclass(frozen=True)
class StoredDocumentContent:
    document_id: int
    filename: str
    chunks: tuple[str, ...]


@dataclass(frozen=True)
class PreparedDocument:
    filename: str
    sha256: str
    full_text: str
    chunks: tuple[str, ...]
    summary: str


class DocumentRepository:
    def __init__(self, store):
        self.store = store

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

        with self.store._connect() as connection:
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

    def update_document_summary(
            self,
            document_id: int,
            summary: str) -> None:
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE documents SET summary = ? WHERE id = ?",
                (summary, document_id),
            )

    def list_document_contents(
            self,
            group_openid: str) -> tuple[StoredDocumentContent, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    d.id AS document_id,
                    d.filename,
                    c.chunk_index,
                    c.content
                FROM documents d
                LEFT JOIN document_chunks c ON c.document_id = d.id
                WHERE d.group_openid = ?
                ORDER BY d.id DESC, c.chunk_index ASC
                """,
                (group_openid,),
            ).fetchall()

        grouped: list[dict[str, object]] = []
        for row in rows:
            document_id = int(row["document_id"])
            if not grouped or grouped[-1]["document_id"] != document_id:
                grouped.append({
                    "document_id": document_id,
                    "filename": str(row["filename"]),
                    "chunks": [],
                })
            if row["content"] is not None:
                grouped[-1]["chunks"].append(str(row["content"]))

        return tuple(
            StoredDocumentContent(
                document_id=int(item["document_id"]),
                filename=str(item["filename"]),
                chunks=tuple(item["chunks"]),
            )
            for item in grouped
        )

    def build_document_context(
            self,
            group_openid: str,
            query: str,
            max_chars: int = MAX_DOCUMENT_CONTEXT_CHARS,
            allow_low_relevance: bool = True) -> str:
        if max_chars <= 0:
            return ""

        with self.store._connect() as connection:
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
            selected = self._select_document_chunks(
                connection,
                group_openid,
                query,
                allow_low_relevance=allow_low_relevance,
            )

        if not documents:
            return ""
        if not selected and not allow_low_relevance:
            return ""

        parts: list[str] = []
        query_terms = self._search_terms(query)
        if selected:
            relevant_limit = (
                max_chars
                if max_chars < 600
                else max(1, int(max_chars * 0.76))
            )
            self._append_context_part(
                parts,
                ("与当前问题相关的文档片段：" if any(
                    term in row['content'].lower() for row in selected for term in query_terms)
                 else "未找到明确相关片段；以下是最新文档参考，相关性较低："),
                relevant_limit,
                allow_truncate=True,
            )
            for index, row in enumerate(selected):
                remaining_entries = len(selected) - index
                available = (
                    relevant_limit - self._context_length(parts)
                    - (1 if parts else 0)
                )
                if available <= 0:
                    break
                header = (
                    f"[文档#{row['document_id']} {row['filename']} / 片段 {row['chunk_index'] + 1}]"
                )
                per_entry = max(1, available // remaining_entries)
                excerpt_limit = max(1, per_entry - len(header) - 1)
                excerpt = self._match_centered_excerpt(
                    row["content"],
                    query_terms,
                    excerpt_limit,
                )
                self._append_context_part(
                    parts,
                    f"{header}\n{excerpt}",
                    relevant_limit,
                    allow_truncate=True,
                )

        overview_rows = []
        seen_document_ids = set()
        for row in [*selected, *documents]:
            document_id = int(row["document_id"] if "document_id" in row.keys() else row["id"])
            if document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            overview_rows.append(row)

        if self._remaining_context_chars(parts, max_chars) >= 30:
            self._append_context_part(
                parts,
                "群内已保存的旅行文档：",
                max_chars,
            )
            for document in overview_rows:
                remaining = self._remaining_context_chars(parts, max_chars)
                if remaining < 20:
                    break
                overview = document["summary"] or document["preview"]
                overview = re.sub(r"\s+", " ", overview).strip()
                filename = document["filename"]
                overview_limit = max(1, min(260, remaining - len(filename) - 4))
                overview = self._match_centered_excerpt(
                    overview,
                    query_terms,
                    overview_limit,
                )
                if not self._append_context_part(
                        parts,
                        f"- {filename}：{overview}",
                        max_chars,
                        allow_truncate=True):
                    break

        return "\n".join(parts)

    def _select_document_chunks(
            self,
            connection: sqlite3.Connection,
            group_openid: str,
            query: str,
            allow_low_relevance: bool = True,
            limit: int = MAX_DOCUMENT_CHUNKS) -> list[sqlite3.Row]:
        limit=max(1,min(int(limit),20))
        candidates: list[sqlite3.Row] = []
        seen_chunk_ids = set()
        vector_scores: dict[int, float] = {}

        fts_terms = self._fts_terms(query)
        if self.store._document_fts_available and fts_terms:
            fts_query = " OR ".join(f'"{term}"' for term in fts_terms)
            try:
                rows = connection.execute(
                    """
                    SELECT
                        c.id AS chunk_id,
                        c.document_id,
                        c.chunk_index,
                        c.content,
                        d.filename,
                        d.preview,
                        d.summary,
                        d.id AS document_order
                    FROM document_chunks_fts
                    JOIN document_chunks c
                      ON c.id = document_chunks_fts.rowid
                    JOIN documents d ON d.id = c.document_id
                    WHERE document_chunks_fts MATCH ?
                      AND d.group_openid = ?
                    ORDER BY bm25(document_chunks_fts) ASC,
                             d.id DESC,
                             c.chunk_index ASC
                    LIMIT ?
                    """,
                    (fts_query, group_openid, MAX_DOCUMENT_CANDIDATES),
                ).fetchall()
                for row in rows:
                    candidates.append(row)
                    seen_chunk_ids.add(row["chunk_id"])
            except sqlite3.OperationalError:
                self.store._document_fts_available = False

        fallback_terms = sorted(
            self._search_terms(query),
            key=lambda term: (-len(term), term),
        )[:MAX_FALLBACK_QUERY_TERMS]
        should_use_fallback = (
            not self.store._document_fts_available
            or not fts_terms
            or not candidates
        )
        if fallback_terms and should_use_fallback:
            score_clauses = []
            score_parameters: list[object] = []
            match_clauses = []
            match_parameters: list[object] = []
            for term in fallback_terms:
                score_clauses.append(
                    "CASE WHEN instr(lower(c.content), ?) > 0 "
                    "THEN ? ELSE 0 END"
                )
                score_parameters.extend((term, max(1, len(term))))
                match_clauses.append("instr(lower(c.content), ?) > 0")
                match_parameters.append(term)

            rows = connection.execute(
                f"""
                SELECT
                    c.id AS chunk_id,
                    c.document_id,
                    c.chunk_index,
                    c.content,
                    d.filename,
                    d.preview,
                    d.summary,
                    d.id AS document_order,
                    ({' + '.join(score_clauses)}) AS match_score
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.group_openid = ?
                  AND ({' OR '.join(match_clauses)})
                ORDER BY match_score DESC,
                         d.id DESC,
                         c.chunk_index ASC
                LIMIT ?
                """,
                (
                    *score_parameters,
                    group_openid,
                    *match_parameters,
                    MAX_DOCUMENT_CANDIDATES,
                ),
            ).fetchall()
            for row in rows:
                if row["chunk_id"] in seen_chunk_ids:
                    continue
                candidates.append(row)
                seen_chunk_ids.add(row["chunk_id"])

        if current_policy().vector_enabled:
            semantic=getattr(self.store,'semantic_index',None)
            if semantic is not None:
                try:
                    matches=semantic.search(group_openid,query,MAX_DOCUMENT_CANDIDATES)
                    self.store.diagnostics.record('retrieval',{'name':'local_embedding','status':'completed',
                                            'candidate_count':len(matches)})
                    vector_rows=[]
                    for chunk_id,score in matches:
                        row=connection.execute('''SELECT c.id AS chunk_id,c.document_id,c.chunk_index,c.content,
                            d.filename,d.preview,d.summary,d.id AS document_order FROM document_chunks c
                            JOIN documents d ON d.id=c.document_id WHERE c.id=? AND d.group_openid=?''',
                            (chunk_id,group_openid)).fetchone()
                        if row:
                            vector_rows.append((score,row))
                except Exception as exc:
                    from core.execution_scope import ExecutionRevoked
                    if isinstance(exc,ExecutionRevoked):
                        raise
                    self.store.diagnostics.record('retrieval',{'name':'local_embedding','status':'failed',
                                            'error_type':type(exc).__name__})
                    vector_rows=[]
            elif not getattr(self.store,'semantic_index_unavailable',False):
                vector_rows=vector_candidates(connection, group_openid, query, MAX_DOCUMENT_CANDIDATES)
            else:
                vector_rows=[]
                self.store.diagnostics.record('retrieval',{'name':'local_embedding','status':'unavailable'})
            for vector_score, row in vector_rows:
                vector_scores[int(row['chunk_id'])] = vector_score
                if row['chunk_id'] not in seen_chunk_ids:
                    candidates.append(row)
                    seen_chunk_ids.add(row['chunk_id'])

        terms = self._search_terms(query)
        ranked = []
        for row in candidates:
            content = row["content"].lower()
            score = sum(
                content.count(term) * max(1, len(term))
                for term in terms
            )
            ranked.append(
                (score, row["document_order"], -row["chunk_index"], row)
            )
        ranked.sort(reverse=True, key=lambda item: item[:3])
        if vector_scores:
            rows_by_id={row['chunk_id']:row for row in candidates}
            lexical=[item[3]['chunk_id'] for item in ranked if item[0]>0]
            hashed=sorted(vector_scores,key=lambda key:(-vector_scores[key],key))
            order=reciprocal_rank_fusion([lexical,hashed])
            return [rows_by_id[key] for key in order[:limit]]
        selected = [
            item[3] for item in ranked if item[0] > 0
        ][:limit]
        if selected:
            return selected

        if not allow_low_relevance:
            return []

        newest = connection.execute(
            """
            SELECT
                c.id AS chunk_id,
                c.document_id,
                c.chunk_index,
                c.content,
                d.filename,
                d.preview,
                d.summary,
                d.id AS document_order
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.group_openid = ?
            ORDER BY d.id DESC, c.chunk_index ASC
            LIMIT 1
            """,
            (group_openid,),
        ).fetchone()
        return [newest] if newest else []

    @staticmethod
    def _fts_terms(text: str) -> tuple[str, ...]:
        normalized = text.lower()
        terms = set(re.findall(r"[a-z0-9]{3,}", normalized))
        for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized):
            if len(sequence) >= 3:
                terms.update(
                    sequence[index:index + 3]
                    for index in range(len(sequence) - 2)
                )
        return tuple(sorted(terms))[:MAX_FTS_QUERY_TERMS]

    @staticmethod
    def _match_centered_excerpt(
            text: str,
            terms: set[str],
            max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text

        normalized = text.lower()
        match_position = None
        for term in sorted(terms, key=lambda value: (-len(value), value)):
            position = normalized.find(term)
            if position >= 0:
                match_position = position
                break

        body_limit = max(1, max_chars - 2)
        if match_position is None:
            start = 0
        else:
            start = max(0, match_position - body_limit // 3)
        end = min(len(text), start + body_limit)
        if end - start < body_limit:
            start = max(0, end - body_limit)

        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        available = max(0, max_chars - len(prefix) - len(suffix))
        return f"{prefix}{text[start:start + available]}{suffix}"

    @staticmethod
    def _context_length(parts: list[str]) -> int:
        return sum(len(part) for part in parts) + max(0, len(parts) - 1)

    @classmethod
    def _remaining_context_chars(
            cls,
            parts: list[str],
            max_chars: int) -> int:
        return max(0, max_chars - cls._context_length(parts))

    @classmethod
    def _append_context_part(
            cls,
            parts: list[str],
            text: str,
            max_chars: int,
            allow_truncate: bool = False) -> bool:
        separator_chars = 1 if parts else 0
        remaining = max_chars - cls._context_length(parts) - separator_chars
        if remaining <= 0:
            return False
        if len(text) > remaining:
            if not allow_truncate:
                return False
            if remaining == 1:
                text = "…"
            else:
                text = text[:remaining - 1].rstrip() + "…"
        parts.append(text)
        return True

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

