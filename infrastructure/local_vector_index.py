from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import re


MODEL_ID = 'hash-ngram-256-v1'
DIMENSIONS = 256


def _features(text: str) -> dict[int, float]:
    normalized = re.sub(r'\s+', ' ', text.lower()).strip()
    tokens = re.findall(r'[a-z0-9]{2,}|[\u4e00-\u9fff]', normalized)
    sequences = list(tokens)
    chinese = ''.join(re.findall(r'[\u4e00-\u9fff]', normalized))
    sequences.extend(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    counts = Counter()
    for token in sequences:
        digest = hashlib.blake2b(token.encode('utf-8'), digest_size=2).digest()
        counts[int.from_bytes(digest, 'big') % DIMENSIONS] += 1
    norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
    return {index: value / norm for index, value in counts.items()}


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(index, 0.0) for index, value in left.items())


def vector_candidates(connection, group_openid: str, query: str, limit: int = 10):
    connection.execute('''CREATE TABLE IF NOT EXISTS document_chunk_vectors (
        chunk_id INTEGER PRIMARY KEY,
        content_hash TEXT NOT NULL,
        model_id TEXT NOT NULL,
        vector_json TEXT NOT NULL,
        FOREIGN KEY(chunk_id) REFERENCES document_chunks(id) ON DELETE CASCADE
    )''')
    rows = connection.execute('''SELECT c.id AS chunk_id,c.document_id,c.chunk_index,c.content,
        d.filename,d.preview,d.summary,d.id AS document_order
        FROM document_chunks c JOIN documents d ON d.id=c.document_id
        WHERE d.group_openid=? ORDER BY d.id DESC,c.chunk_index LIMIT 2000''',
        (group_openid,)).fetchall()
    query_vector = _features(query)
    if not query_vector:
        return []
    ranked = []
    for row in rows:
        digest = hashlib.sha256(row['content'].encode('utf-8')).hexdigest()
        stored = connection.execute('''SELECT content_hash,model_id,vector_json
            FROM document_chunk_vectors WHERE chunk_id=?''', (row['chunk_id'],)).fetchone()
        if stored and stored['content_hash'] == digest and stored['model_id'] == MODEL_ID:
            vector = {int(key): float(value) for key, value in json.loads(stored['vector_json']).items()}
        else:
            vector = _features(row['content'])
            connection.execute('''INSERT INTO document_chunk_vectors VALUES (?,?,?,?)
                ON CONFLICT(chunk_id) DO UPDATE SET content_hash=excluded.content_hash,
                model_id=excluded.model_id,vector_json=excluded.vector_json''',
                (row['chunk_id'], digest, MODEL_ID, json.dumps(vector, separators=(',', ':'))))
        score = _cosine(query_vector, vector)
        if score >= 0.25:
            ranked.append((score, row))
    ranked.sort(key=lambda item: (item[0], item[1]['document_order'], -item[1]['chunk_index']), reverse=True)
    return ranked[:limit]
