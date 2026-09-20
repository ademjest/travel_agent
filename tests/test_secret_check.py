from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.check_secrets import scan


class SecretCheckTests(unittest.TestCase):
    def test_forced_env_and_secret_in_unrelated_file_are_blocked_without_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
            git('init')
            secret = 'fixture-' + 'x' * 25
            (root/'.env').write_text('SEARCH_API_KEY=' + secret)
            (root/'.gitignore').write_text('.env\n.env.*\n!.env.example\n')
            (root/'.env.example').write_text('SEARCH_API_KEY=\n')
            git('add', '.')
            self.assertEqual(scan(root), [])
            git('add', '-f', '.env')
            findings = scan(root)
            self.assertTrue(any(row['rule'] == 'forbidden-config-file' for row in findings))
            self.assertNotIn(secret, str(findings))
            (root/'report.md').write_text('accidental: ' + secret)
            git('add', 'report.md')
            self.assertTrue(any(row['path'] == 'report.md' for row in scan(root)))
