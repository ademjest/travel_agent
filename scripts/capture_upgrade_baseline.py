"""Capture source hashes and tool versions without reading application data or secrets."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ('adapters', 'agents', 'app', 'core', 'infrastructure', 'services', 'tools',
               'evaluation', 'tests', 'scripts', 'skills', '.github', '.githooks', 'deploy',
               'frontend/src', 'frontend/public', 'prompts', 'docs/guides')
SOURCE_SUFFIXES = {'.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.css', '.json',
                   '.md', '.txt', '.html', '.yml', '.yaml', '.toml', '.sql', '.sh', '.ps1', '.cmd'}


def source_paths(root=None, exclude=None):
    root = (root or ROOT).resolve()
    candidates = set()
    for folder in SOURCE_DIRS:
        for path in (root / folder).rglob('*'):
            relative = path.relative_to(root)
            if (not path.is_file() or set(relative.parts) & {'__pycache__', 'node_modules', 'dist', '.git'}
                    or relative.as_posix().startswith('scripts/archive/')
                    or (path.name.startswith('.env') and path.name != '.env.example')):
                continue
            if (path.suffix in SOURCE_SUFFIXES or path.name == 'Dockerfile'
                    or folder in {'.githooks', 'frontend/public'}
                    or relative.as_posix().startswith('tests/fixtures/')):
                candidates.add(path)
    candidates.update(path for path in root.iterdir() if path.is_file() and
                      (path.suffix in {'.py', '.cmd', '.ps1', '.sh'} or
                       path.name.startswith(('requirements', 'constraints')) and path.suffix == '.txt'))
    candidates.update(path for path in (root/'frontend').glob('*') if path.is_file() and
                      path.suffix in {'.ts', '.js', '.mjs', '.cjs', '.json', '.html'})
    candidates.update(path for path in (root/'evals').glob('*.json') if
                      '_cases_' in path.name or path.name in {'v3-intent-probes.json', 'v3-live-probe-plan.json'})
    candidates.update(root/name for name in ('README.md', 'AGENTS.md', 'CLAUDE.md', '.gitignore',
                      '.dockerignore', '.env.example', 'data/official_sources.json',
                      'data/attraction_aliases.json') if (root/name).is_file())
    result = []
    for path in sorted(candidates, key=lambda p:p.relative_to(root).as_posix()):
        if not path.resolve().is_relative_to(root):
            raise ValueError('Source input resolves outside the project: '+str(path.relative_to(root)))
        if exclude is None or path.resolve() != Path(exclude).resolve():
            result.append(path)
    return result


def capture(exclude=None):
    files = [{'path':path.relative_to(ROOT).as_posix(), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in source_paths(exclude=exclude)]
    def command(*args):
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else 'unavailable'
    versions = {}
    for name in ('openai', 'fastapi', 'python-multipart', 'requests', 'python-dotenv'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = 'missing'
    digest = hashlib.sha256(json.dumps(sorted(files,key=lambda f:f['path']),sort_keys=True).encode()).hexdigest()
    return {'version': 'release-baseline-v2', 'source_sha256':digest, 'head': command('git', 'rev-parse', 'HEAD'),
            'git_status': command('git', 'status', '--short'), 'python': platform.python_version(),
            'node': command('node', '--version'), 'dependencies': versions, 'source_files': files}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(capture(args.output.resolve()), ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(args.output))
