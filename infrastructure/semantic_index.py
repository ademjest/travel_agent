"""Optional semantic-index experiment; embedders are injected, never downloaded implicitly."""
import hashlib
import json
import math
import asyncio
import logging
from datetime import datetime, timezone


def normalized(values):
    if not isinstance(values,(list,tuple)) or not 1 <= len(values) <= 4096:
        raise ValueError('Embedding维度无效。')
    vector=[float(value) for value in values]
    if not all(math.isfinite(value) for value in vector):
        raise ValueError('Embedding含非有限值。')
    norm=math.sqrt(sum(value*value for value in vector))
    if not norm:
        raise ValueError('Embedding为零向量。')
    return [value/norm for value in vector]


class SemanticIndex:
    def __init__(self,store,embedder,model,dimensions,provider='local-experiment',minimum_similarity=.35):
        self.store,self.embedder,self.dimensions=store,embedder,dimensions
        self.namespace=hashlib.sha256(f'{provider}:{model}:{dimensions}'.encode()).hexdigest()
        self.model=model
        self.calls=0
        self.minimum_similarity=minimum_similarity
        self.states={}

        with store._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS semantic_chunk_vectors (
                namespace TEXT NOT NULL,chunk_id INTEGER NOT NULL,source_hash TEXT NOT NULL,
                vector_json TEXT NOT NULL,PRIMARY KEY(namespace,chunk_id),
                FOREIGN KEY(chunk_id) REFERENCES document_chunks(id) ON DELETE CASCADE)''')

    def embed(self,texts,query=False):
        self.calls+=1
        values=(self.embedder.encode_queries(texts) if query and hasattr(self.embedder,'encode_queries')
                else self.embedder(texts))
        if len(values)!=len(texts):
            raise ValueError('Embedding返回数量错误。')
        result=[normalized(vector) for vector in values]
        if any(len(vector)!=self.dimensions for vector in result):
            raise ValueError('Embedding维度与命名空间不一致。')
        return result

    def update(self,scope,batch_size=32):
        """Explicit bounded index maintenance, never triggered by a user search."""
        if type(batch_size) is not int or not 1<=batch_size<=32:
            raise ValueError('索引更新每批必须为1到32个片段。')
        with self.store._connect() as db:
            rows=[dict(row) for row in db.execute('''SELECT c.id,c.content FROM document_chunks c
                JOIN documents d ON d.id=c.document_id WHERE d.group_openid=? ORDER BY c.id''',(scope,))]
            stored={row['chunk_id']:dict(row) for row in db.execute('''SELECT v.* FROM semantic_chunk_vectors v
                JOIN document_chunks c ON c.id=v.chunk_id JOIN documents d ON d.id=c.document_id
                WHERE v.namespace=? AND d.group_openid=?''',(self.namespace,scope))}
        changed=[row for row in rows if row['id'] not in stored or stored[row['id']]['source_hash']!=hashlib.sha256(row['content'].encode()).hexdigest()]
        remaining=max(0,len(changed)-batch_size)
        changed=changed[:batch_size]
        stamp=datetime.now(timezone.utc).isoformat()
        self.states[scope]={'state':'building' if changed else 'ready' if rows else 'empty',
                           'total':len(rows),'indexed':len(rows)-remaining-len(changed),
                           'pending':remaining+len(changed),'checked_at':stamp,'error_type':''}
        if changed:
            try:
                vectors=self.embed([row['content'] for row in changed])
            except Exception as error:
                self.states[scope].update(state='failed',error_type=type(error).__name__)
                raise
            accepted=0
            with self.store._connect() as db:
                for row,vector in zip(changed,vectors):
                    current=db.execute('''SELECT c.content FROM document_chunks c JOIN documents d ON d.id=c.document_id
                        WHERE c.id=? AND d.group_openid=?''',(row['id'],scope)).fetchone()
                    if not current or current['content']!=row['content']:
                        continue
                    digest=hashlib.sha256(row['content'].encode()).hexdigest()
                    raw=json.dumps(vector)
                    db.execute('''INSERT INTO semantic_chunk_vectors VALUES (?,?,?,?)
                        ON CONFLICT(namespace,chunk_id) DO UPDATE SET source_hash=excluded.source_hash,vector_json=excluded.vector_json''',
                        (self.namespace,row['id'],digest,raw))
                    stored[row['id']]={'source_hash':digest,'vector_json':raw}
                    accepted+=1
            self.states[scope].update(state='partial' if remaining or accepted<len(changed) else 'ready',
                indexed=self.states[scope]['indexed']+accepted,pending=remaining+len(changed)-accepted)
        return {'processed':len(changed),'remaining':remaining,'namespace':self.namespace}

    def snapshot(self,scope):
        return {'namespace':self.namespace,'model':self.model,
                'encoder':self.embedder.snapshot() if hasattr(self.embedder,'snapshot') else {'state':'unknown'},
                'index':dict(self.states.get(scope,{'state':'awaiting_check','checked_at':None}))}

    def search(self,scope,query,k=10,minimum_similarity=None):
        minimum_similarity=self.minimum_similarity if minimum_similarity is None else minimum_similarity
        with self.store._connect() as db:
            stored={row['chunk_id']:dict(row) for row in db.execute('''SELECT v.* FROM semantic_chunk_vectors v
                JOIN document_chunks c ON c.id=v.chunk_id JOIN documents d ON d.id=c.document_id
                WHERE v.namespace=? AND d.group_openid=?''',(self.namespace,scope))}
        if not stored:
            state=self.states.get(scope,{}).get('state','awaiting_check')
            if state=='failed':
                raise RuntimeError('local_index_failed')
            return []
        query_vector=self.embed([query],query=True)[0]
        scored=[]
        # Re-read sources after model work: deletions/updates must not reappear.
        with self.store._connect() as db:
            for row in db.execute('''SELECT c.id,c.content FROM document_chunks c JOIN documents d ON d.id=c.document_id
                WHERE d.group_openid=? ORDER BY c.id''',(scope,)):
                entry=stored.get(row['id'])
                if not entry or entry['source_hash']!=hashlib.sha256(row['content'].encode()).hexdigest():
                    continue
                vector=json.loads(entry['vector_json'])
                score=sum(a*b for a,b in zip(query_vector,vector))
                if score>=minimum_similarity:
                    scored.append((row['id'],score))
        return sorted(scored,key=lambda pair:(-pair[1],pair[0]))[:k]

    async def run(self):
        """Maintain new documents in the background, bounded to 32 chunks per scope/pass."""
        while True:
            with self.store._connect() as db:
                scopes=[row[0] for row in db.execute('SELECT DISTINCT group_openid FROM documents')]
            for scope in scopes:
                try:
                    await asyncio.to_thread(self.update,scope)
                except Exception as error:
                    logging.getLogger(__name__).warning('Local index update failed: %s',type(error).__name__)
                await asyncio.sleep(0)
            await asyncio.sleep(30)
