"""Capture source hashes and tool versions without reading application data or secrets."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def capture(exclude=None):
    files = []
    for folder in ('adapters', 'agents', 'app', 'core', 'infrastructure', 'services', 'tools', 'tests', 'scripts', 'frontend/src', 'evals'):
        for path in sorted((ROOT / folder).rglob('*')):
            if (path.is_file() and path.resolve()!=exclude and path.suffix in {'.py', '.ts', '.tsx', '.css', '.json'}
                    and '__pycache__' not in path.parts):
                files.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    for path in sorted([*ROOT.glob('*.py'),*ROOT.glob('requirements*.txt'),ROOT/'frontend/package.json',ROOT/'frontend/package-lock.json']):
        if path.is_file():
            files.append({'path':path.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    def command(*args):
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else 'unavailable'
    versions = {}
    for name in ('openai', 'fastapi', 'python-multipart', 'requests', 'python-dotenv'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = 'missing'
    return {'version': 'v3-b0', 'head': command('git', 'rev-parse', 'HEAD'),
            'git_status': command('git', 'status', '--short'), 'python': platform.python_version(),
            'node': command('node', '--version'), 'dependencies': versions, 'source_files': files}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(capture(args.output.resolve()), ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(args.output))
