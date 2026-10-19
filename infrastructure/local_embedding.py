"""Pinned BGE-small Chinese ONNX encoder; inference never accesses the network."""
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from datetime import datetime, timezone
import importlib.util

MODEL_REPO = 'Xenova/bge-small-zh-v1.5'
MODEL_REVISION = '75c43b069aac4d136ba6bc1122f995fedcfd2781'
MODEL_SHA256 = '15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc'
TOKENIZER_SHA256 = '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26'
QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：'
ENCODING_VERSION = 'bge-zh-cls-window-510-stride-384-mean-v1'
DIMENSIONS = 512
logger = logging.getLogger(__name__)


class LocalEmbedding:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        manifest = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8'))
        if (manifest['repo'] != MODEL_REPO or manifest['revision'] != MODEL_REVISION
                or manifest['files']['onnx/model_quantized.onnx'] != MODEL_SHA256
                or manifest['files']['tokenizer.json'] != TOKENIZER_SHA256):
            raise ValueError('本地模型清单不匹配固定版本。')
        self.files = manifest['files']
        prefix_hash = hashlib.sha256(QUERY_PREFIX.encode()).hexdigest()[:12]
        self.model_id = f'{MODEL_REPO}@{MODEL_REVISION}:{ENCODING_VERSION}:{prefix_hash}'
        self.lock = threading.Lock()
        self.session = self.tokenizer = None
        self.state = {'state': 'not_loaded', 'error_type': '', 'checked_at': None}
        self.retry_at = 0.0

    def snapshot(self):
        return {**self.state, 'model': self.model_id}

    def _load(self):
        if self.session is not None:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer
        for name in ('onnx/model_quantized.onnx', 'tokenizer.json'):
            if hashlib.sha256((self.folder / name).read_bytes()).hexdigest() != self.files[name]:
                raise ValueError('本地模型文件校验失败。')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.tokenizer = Tokenizer.from_file(str(self.folder / 'tokenizer.json'))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.session = ort.InferenceSession(str(self.folder / 'onnx/model_quantized.onnx'),
                                            sess_options=options, providers=['CPUExecutionProvider'])

    def _encode(self, texts, query=False):
        with self.lock:
            if time.monotonic() < self.retry_at:
                raise RuntimeError('local_encoder_cooldown')
            try:
                self.state = {'state': 'loading', 'error_type': '', 'checked_at': datetime.now(timezone.utc).isoformat()}
                import numpy as np
                self._load()
                self.state = {**self.state, 'state': 'ready'}
            except Exception as error:
                self.state = {**self.state, 'state': 'failed', 'error_type': type(error).__name__}
                self.retry_at = time.monotonic() + 60
                raise
            vectors = []
            for text in texts:
                ids = self.tokenizer.encode((QUERY_PREFIX if query else '') + text).ids
                if query and len(ids) > 512:
                    raise ValueError('语义查询超过模型长度，使用词法回退。')
                body = ids[1:-1]
                windows = [ids] if len(ids) <= 512 else [
                    [ids[0]] + body[offset:offset + 510] + [ids[-1]]
                    for offset in range(0, len(body), 384)]
                # A long stored chunk is fully represented, never silently truncated.
                encoded = []
                for window in windows:
                    inputs = {'input_ids': np.asarray([window], dtype=np.int64),
                              'attention_mask': np.ones((1, len(window)), dtype=np.int64),
                              'token_type_ids': np.zeros((1, len(window)), dtype=np.int64)}
                    values = self.session.run(None, {field.name: inputs[field.name] for field in self.session.get_inputs()})
                    cls = values[0][0, 0].astype(np.float32)
                    norm = np.linalg.norm(cls)
                    if cls.shape != (DIMENSIONS,) or not np.isfinite(cls).all() or norm == 0:
                        raise ValueError('本地向量输出无效。')
                    encoded.append(cls / norm)
                vector = np.mean(encoded, axis=0)
                norm = np.linalg.norm(vector)
                if not np.isfinite(vector).all() or norm == 0:
                    raise ValueError('聚合向量无效。')
                vectors.append((vector / norm).tolist())
            return vectors

    def __call__(self, texts):
        return self._encode(texts)

    def encode_queries(self, texts):
        return self._encode(texts, query=True)


def configure_local_index(store):
    from core.model_policy import current_policy
    from infrastructure.semantic_index import SemanticIndex
    store.semantic_status = {'state': 'disabled', 'error_type': ''}
    if not current_policy().vector_enabled:
        return
    # The historical hash experiment is never a production fallback for this encoder.
    store.semantic_index_unavailable = True
    store.semantic_index = None
    try:
        folder = os.getenv('AI_EMBEDDING_MODEL_DIR', '').strip()
        if not folder:
            raise ValueError('未设置本地模型目录。')
        folder = Path(folder)
        if not folder.is_absolute():
            folder = Path(__file__).resolve().parents[1] / folder
        threshold = float(os.getenv('AI_EMBEDDING_MIN_SIMILARITY', '0.5'))
        if not 0 <= threshold <= 1:
            raise ValueError('相似度门槛应为0到1。')
        embedder = LocalEmbedding(folder)
        if any(importlib.util.find_spec(name) is None for name in ('numpy', 'onnxruntime', 'tokenizers')):
            store.semantic_status = {'state': 'dependency_missing', 'error_type': 'ImportError'}
            return
        store.semantic_index = SemanticIndex(store, embedder, embedder.model_id, DIMENSIONS,
                                             provider='local-onnx', minimum_similarity=threshold)
        store.semantic_index_unavailable = False
        store.semantic_status = {'state': 'configured', 'error_type': ''}
    except (OSError, ValueError, KeyError, TypeError) as error:
        store.semantic_status = {'state': 'invalid_configuration', 'error_type': type(error).__name__}
        logger.warning('Local embedding unavailable; lexical retrieval only: %s', type(error).__name__)
