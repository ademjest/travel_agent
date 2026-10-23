"""Exercise actual UTF-8 file handlers without starting network services."""
from pathlib import Path
import logging
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


class RuntimeLoggingTests(unittest.TestCase):
    def test_web_application_and_uvicorn_logs_share_target_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            code = textwrap.dedent('''
                from pathlib import Path
                from types import SimpleNamespace
                from unittest.mock import patch
                import logging, sys
                import run_web
                root = Path(sys.argv[1])
                settings = SimpleNamespace(host='127.0.0.1', port=0, open_browser=False)
                def emit(**kwargs):
                    logging.getLogger('services.example').warning('中文业务日志')
                    logging.getLogger('uvicorn.error').info('server-started')
                    logging.getLogger('uvicorn.access').info('request-access')
                with patch('run_web.log_directory', return_value=root), \\
                     patch('run_web.WebSettings.from_env', return_value=settings), \\
                     patch('run_web.create_web_app', return_value=object()), \\
                     patch('run_web.uvicorn.Server') as server:
                    server.return_value.run.side_effect = emit
                    run_web.main()
                logging.shutdown()
            ''')
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', code, directory],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = (Path(directory) / 'travel-web.log').read_text(encoding='utf-8')
            for message in ('中文业务日志', 'server-started', 'request-access'):
                self.assertIn(message, content)
                self.assertIn(message, result.stderr)

    def test_qq_sdk_writes_to_shared_directory_and_keeps_rotation_settings(self):
        import run_bot
        class StopAfterLogging(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'logs'
            with patch('run_bot.log_directory', return_value=target), \
                 patch('botpy.Client.__init__', side_effect=StopAfterLogging) as initialize:
                with self.assertRaises(StopAfterLogging):
                    run_bot.TravelRiskBot(settings=None)
            config = initialize.call_args.kwargs['ext_handlers']
            handler = run_bot.logging.get_handler(config)
            try:
                handler.handle(logging.LogRecord('botpy', logging.INFO, __file__, 1, 'QQ中文日志', (), None))
            finally:
                handler.close()
            self.assertIn('QQ中文日志', (target / 'botpy.log').read_text(encoding='utf-8'))
            self.assertEqual(config['backupCount'], run_bot.logging.DEFAULT_FILE_HANDLER['backupCount'])


if __name__ == '__main__':
    unittest.main()
