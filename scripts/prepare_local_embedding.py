"""Explicitly download the pinned public ONNX package; never run by the server."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from infrastructure.local_embedding import MODEL_REPO, MODEL_REVISION, MODEL_SHA256


def prepare(folder):
    import requests
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    hashes = {}
    names = {'onnx/model_quantized.onnx': MODEL_SHA256,
             'tokenizer.json': 'cdb3043fc938fc918c06e66cf704c2ba58f88747',
             'config.json': 'd590a6643f5dcdd0cfb0477e3c291488c16fc2d7'}
    for name, expected in names.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with requests.get(f'https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}',
                              stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                temporary = path.with_suffix(path.suffix + '.part')
                with temporary.open('wb') as output:
                    size = 0
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > 30 * 1024 * 1024:
                            raise ValueError('模型文件超过固定包预期大小。')
                        output.write(chunk)
                data = temporary.read_bytes()
                digest = (hashlib.sha256(data).hexdigest() if len(expected) == 64 else
                          hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest())
                if digest != expected:
                    raise ValueError(f'文件版本校验失败：{name}')
                temporary.replace(path)
        data = path.read_bytes()
        digest = (hashlib.sha256(data).hexdigest() if len(expected) == 64 else
                  hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest())
        if digest != expected:
            raise ValueError(f'已有文件版本校验失败：{name}')
        hashes[name] = hashlib.sha256(data).hexdigest()
    manifest = {'repo': MODEL_REPO, 'revision': MODEL_REVISION,
                'base_model': 'BAAI/bge-small-zh-v1.5', 'base_revision': '7999e1d3359715c523056ef9478215996d62a620',
                'base_license': 'MIT', 'format': 'ONNX quantized, third-party conversion', 'files': hashes}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'model_dir': str(folder.resolve()), **manifest}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.models/bge-small-zh-v1.5-onnx')
    prepare(parser.parse_args().output)
