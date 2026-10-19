from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.execution_scope import CURRENT_EXECUTION, ExecutionScope
from infrastructure.memory_store import MemoryStore
from infrastructure.public_http import PublicHTTPError, request_public


def response(status=200, **headers):
    return Mock(status=status, headers={'Content-Type':'application/json',**headers}, stream=Mock(return_value=[b'{}']))


class PublicReadReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.pool=Mock()
        for target,value in [('infrastructure.public_http.resolve_host',('8.8.8.8',)),
                             ('infrastructure.public_http.urllib3.HTTPSConnectionPool',self.pool)]:
            p=patch(target,return_value=value)
            p.start(); self.addCleanup(p.stop)

    def test_transient_failure_recovers_without_hidden_nested_retries(self):
        self.pool.urlopen.side_effect=[response(503),response()]
        pause=Mock()
        self.assertEqual(request_public('https://example.test',sleeper=pause)[0],b'{}')
        self.assertEqual(self.pool.urlopen.call_count,2)
        self.assertEqual(pause.call_count,1)

    def test_permanent_error_is_not_retried(self):
        self.pool.urlopen.return_value=response(403)
        with self.assertRaises(PublicHTTPError) as caught:
            request_public('https://example.test',sleeper=Mock())
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.pool.urlopen.call_count,1)

    def test_long_retry_after_never_sleeps_early(self):
        self.pool.urlopen.return_value=response(429,**{'Retry-After':'120'})
        pause=Mock()
        with self.assertRaises(PublicHTTPError) as caught:
            request_public('https://example.test',sleeper=pause)
        self.assertEqual(caught.exception.retry_after_seconds,120)
        pause.assert_not_called()

    def test_redirects_share_three_physical_requests_with_retries(self):
        self.pool.urlopen.side_effect=[response(302,Location='https://second.test'),
                                      response(302,Location='https://third.test'),response(503)]
        with self.assertRaises(PublicHTTPError):
            request_public('https://example.test',redirects=2,sleeper=Mock())
        self.assertEqual(self.pool.urlopen.call_count,3)

    def test_persisted_limit_and_success_checkpoint_survive_new_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'test.db')
            store.inbox.submit('event','onebot','scope','owner',{})
            job=store.inbox.claim()
            store.begin_event('event')
            token=CURRENT_EXECUTION.set(ExecutionScope(str(store.database_path),job['id'],job['claim_token']))
            try:
                self.pool.urlopen.return_value=response(503)
                for _ in range(2):
                    with self.assertRaises(PublicHTTPError):
                        request_public('https://failure.test',sleeper=Mock())
                self.assertEqual(self.pool.urlopen.call_count,3)
                self.pool.urlopen.return_value=response()
                first=request_public('https://success.test')
                count=self.pool.urlopen.call_count
                self.assertEqual(request_public('https://success.test'),first)
                self.assertEqual(self.pool.urlopen.call_count,count)
            finally:
                CURRENT_EXECUTION.reset(token)
