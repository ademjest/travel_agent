import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import capture_upgrade_baseline as manifest
from scripts import run_release_checks as release


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = [
            'run_web.py', 'requirements-web.txt', 'frontend/package.json',
            'frontend/package-lock.json', 'frontend/vite.config.ts',
            'frontend/tsconfig.json', 'frontend/index.html',
            'frontend/src/nested/widget.ts', 'frontend/public/icon.svg',
            '.github/workflows/tests.yml', '.githooks/pre-commit',
            'deploy/napcat/Dockerfile', 'deploy/napcat/compose.yml',
            'start-web.cmd', '.dockerignore', 'README.md',
            'docs/guides/qq.md', 'data/official_sources.json', 'data/attraction_aliases.json',
            'tests/fixtures/note.txt', 'evaluation/support.py',
        ]
        for name in self.inputs:
            self.write(name, '# fixture\n')
        for name in ('.env', 'data/web/travel_bot.db', 'data/private.json',
                     'frontend/node_modules/pkg/index.js', 'frontend/dist/index.html',
                     'core/__pycache__/file.pyc', '.upgrade-testing/old/core/example.py'):
            self.write(name, 'not source')

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def capture(self):
        with patch.object(manifest, 'ROOT', self.root), patch.object(
                manifest.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='fixture')):
            return manifest.capture()

    def test_complete_build_and_runtime_inputs_without_private_data(self):
        paths = {row['path'] for row in self.capture()['source_files']}
        self.assertEqual(paths, set(self.inputs))

    def test_each_build_configuration_and_nested_source_changes_identity(self):
        previous = self.capture()['source_sha256']
        for index, name in enumerate(('frontend/vite.config.ts', 'frontend/tsconfig.json',
                                      'deploy/napcat/Dockerfile', 'frontend/src/nested/widget.ts')):
            self.write(name, f'# changed {index}\n')
            current = self.capture()['source_sha256']
            self.assertNotEqual(current, previous, name)
            previous = current

    def test_snapshot_order_and_output_exclusion_are_stable(self):
        first = self.capture()
        self.assertEqual(first['source_sha256'], self.capture()['source_sha256'])
        self.assertEqual([r['path'] for r in first['source_files']],
                         sorted(r['path'] for r in first['source_files']))


class ReleaseRunnerTests(unittest.TestCase):
    def test_child_failure_is_recorded_and_archive_keeps_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'input.py').write_text('value=1\n',encoding='utf-8')
            output=root/'evidence'
            source={'head':'fixture','source_sha256':'fixed','source_files':[
                {'path':'input.py','sha256':hashlib.sha256((root/'input.py').read_bytes()).hexdigest()}]}
            jobs={'backend':([sys.executable,'-c','raise SystemExit(3)'],root)}
            with patch.object(release,'ROOT',root), patch.object(release,'capture',return_value=source), \
                    patch.object(release,'check_commands',return_value=jobs):
                self.assertFalse(release.run(output,['backend']))
            report=json.loads((output/'index.json').read_text(encoding='utf-8'))
            self.assertEqual(report['checks']['backend']['exit_code'],3)
            self.assertFalse(report['passed'])
            self.assertFalse(report['complete_suite'])
            self.assertTrue((output/'source.zip').is_file())

    def test_timeout_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            _,result=release.execute_check('architecture',[sys.executable,'-c','import time; time.sleep(2)'],
                                          root,root,'fixture',0.05)
            self.assertEqual(result['status'],'timed_out')
            self.assertIsNone(result['exit_code'])

    def test_shared_catalog_has_read_replay_and_isolated_build_output(self):
        jobs=release.check_commands(Path('isolated-output').resolve())
        self.assertEqual(set(jobs),set(release.CHECK_NAMES))
        self.assertIn('read_only',jobs)
        self.assertIn('architecture',jobs)
        command,_=jobs['build']
        self.assertEqual(Path(command[-1]).name,'frontend-dist')
        self.assertIn('--outDir',command)
