from pathlib import Path
import json
import logging
import tempfile
import unittest
import sys
from infrastructure.memory_store import MemoryStore
from infrastructure.execution_diagnostics import metadata
from core.logging_config import WorkflowFormatter,SafeLogFilter


class WorkflowLoggingTests(unittest.TestCase):
    def test_exception_location_survives_both_handlers_without_exception_values(self):
        try:raise KeyError('private-secret')
        except KeyError:
            record=logging.LogRecord('app.bot_application',logging.ERROR,__file__,1,'Unexpected business error',(),sys.exc_info())
        for console in (False,True):
            self.assertTrue(SafeLogFilter(console=console).filter(record))
            self.assertIn('KeyError',record.getMessage())
            self.assertIn('tests/test_workflow_logging.py:',record.getMessage())
            self.assertNotIn('private-secret',record.getMessage())
            self.assertIsNone(record.exc_info)

    def test_access_errors_survive_file_filter_then_console_filter(self):
        record=logging.LogRecord('uvicorn.access',logging.INFO,__file__,1,'%s %s %s %s %d',
            ('127.0.0.1:1000','GET','/api/data?token=private','1.1',503),None)
        self.assertTrue(SafeLogFilter().filter(record))
        self.assertTrue(SafeLogFilter(console=True).filter(record))
        self.assertEqual(record.levelno,logging.ERROR)
        self.assertIn('503',record.getMessage())
        self.assertNotIn('private',record.getMessage())

    def test_file_time_is_utc_and_display_time_is_beijing(self):
        record=logging.LogRecord('travel.workflow',logging.INFO,__file__,1,'safe',(),None);record.created=0
        self.assertTrue(WorkflowFormatter('%(asctime)s',zone='UTC').format(record).endswith('00:00:00+00:00'))
        self.assertTrue(WorkflowFormatter('%(asctime)s').format(record).endswith('08:00:00+08:00'))
    def test_one_lifecycle_record_and_safe_chinese_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'events.db')
            event='web:group:scope:event'
            store.inbox.submit(event,'web','scope','owner',{'content':'私密地址与搜索原话'},False)
            with self.assertLogs('travel.workflow',level=logging.INFO) as capture:
                for status in ('received','claimed','output_prepared'):
                    for _ in range(2):store.diagnostics.record('request',{'event_key':event,'status':status,'origin':'私密地址',
                        'operation_key':'https://secret.example/private'})
                for _ in range(2):store.diagnostics.record('delivery',{'event_key':event,'status':'sent','outbox_id':1})
            with store._connect() as db:
                rows=db.execute('SELECT stage,details_json FROM execution_diagnostics WHERE event_key=?',(event,)).fetchall()
            self.assertEqual(len(rows),4)
            self.assertEqual(len(capture.records),4)
            output='\n'.join(capture.output)
            self.assertIn('等待投递',output);self.assertIn('已发送',output)
            self.assertNotIn('secret.example',output);self.assertNotIn('私密地址',output)
            self.assertNotIn('secret.example',json.dumps([r['details_json'] for r in rows]))

    def test_counts_do_not_invent_remote_http_requests(self):
        clean=metadata({'logical_count':1,'mcp_submissions':3,'http_requests':None,'provider_alias':'rail-remote'})
        self.assertEqual(clean['logical_count'],1)
        self.assertEqual(clean['mcp_submissions'],3)
        self.assertIsNone(clean['http_requests'])
