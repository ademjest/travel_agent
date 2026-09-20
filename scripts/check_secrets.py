"""Check staged (default) or tracked content without printing matched secret values."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from dotenv import dotenv_values


PATTERNS = [re.compile(rb'\b(?:sk-[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})'),
            re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]


def scan(root, all_tracked=False, push=False):
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout
    known = [value.encode() for key, value in dotenv_values(root / '.env').items()
             if re.search(r'KEY|TOKEN|SECRET|PASSWORD', key) and value and len(value) >= 8
             and not value.startswith('replace_with_')]
    problems = set()

    def inspect(path, blob):
        name = Path(path).name
        if name.startswith('.env') and name != '.env.example':
            problems.add((path, 'forbidden-config-file'))
        if any(value in blob for value in known) or any(pattern.search(blob) for pattern in PATTERNS):
            problems.add((path, 'potential-secret'))
        if name == '.env.example':
            for line in blob.decode('utf-8', 'replace').splitlines():
                match = re.match(r'([A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD))=(.*)', line)
                if match and match[2].strip(" '\"") and not match[2].strip(" '\"").startswith('replace_with_'):
                    problems.add((path, 'non-placeholder-template'))

    paths = git('ls-files', '-z') if all_tracked else git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z')
    for raw in paths.split(b'\0'):
        if not raw: continue
        path = raw.decode('utf-8')
        inspect(path, git('show', ':' + path))
    if push:
        # Check commits reachable locally but not covered by remote-tracking refs.
        commits = git('rev-list', '--branches', '--not', '--remotes').decode().splitlines()
        for commit in commits:
            for raw in git('ls-tree', '-r', '--name-only', '-z', commit).split(b'\0'):
                if raw:
                    path = raw.decode('utf-8')
                    inspect(path, git('show', commit + ':' + path))
    return [{'path': path, 'rule': rule} for path, rule in sorted(problems)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all-tracked', action='store_true')
    parser.add_argument('--push', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        problems = scan(root, args.all_tracked, args.push)
    except Exception:
        print('Secret check could not complete; commit/push should be stopped.', file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps({'passed': not problems, 'findings': problems}, ensure_ascii=False))
    raise SystemExit(1 if problems else 0)
