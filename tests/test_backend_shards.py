from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_backend_tests as shards
from scripts import run_release_checks as release


class BackendShardTests(unittest.TestCase):
    def test_partitions_cover_all_ids_once_and_ignore_discovery_order(self):
        ids=[f'test_fixture.Case.test_{i:02}' for i in range(17)]
        groups=[shards.partition(list(reversed(ids)),i,4) for i in range(4)]
        self.assertEqual(sorted(value for group in groups for value in group),ids)
        self.assertEqual(len({value for group in groups for value in group}),len(ids))
        self.assertEqual(groups[2],shards.partition(ids,2,4))

    def test_invalid_partition_or_duplicate_discovery_is_rejected(self):
        for index,count in ((0,0),(-1,4),(4,4)):
            with self.subTest(index=index,count=count),self.assertRaises(ValueError):
                shards.partition(['one'],index,count)
        with self.assertRaises(ValueError):shards.partition(['one','one'],0,4)

    def run_fixture(self,fail=False,changed=False):
        calls=[]
        class Fixture(unittest.TestCase):
            def test_first(self):
                calls.append('first')
                if fail:self.fail('injected assertion failure')
            def test_second(self):calls.append('second')
        cases=[Fixture('test_first'),Fixture('test_second')]
        with tempfile.TemporaryDirectory() as folder,patch.object(shards,'discover',return_value=cases),\
                patch.object(shards,'source_identity',side_effect=['before','after' if changed else 'before']),\
                redirect_stderr(io.StringIO()),redirect_stdout(io.StringIO()):
            result=shards.run(Path(folder)/'backend-shard.json')
        return result,calls

    def test_runner_records_every_actual_test_without_silent_exclusions(self):
        result,calls=self.run_fixture()
        self.assertTrue(result['passed']);self.assertEqual(calls,['first','second'])
        self.assertEqual(result['all_test_ids'],result['executed_ids'])
        self.assertEqual(result['tests'],2)

    def test_failed_assertion_or_source_drift_cannot_pass(self):
        failed,_=self.run_fixture(fail=True)
        self.assertFalse(failed['passed']);self.assertEqual(len(failed['failures']),1)
        changed,_=self.run_fixture(changed=True)
        self.assertFalse(changed['passed']);self.assertFalse(changed['source_unchanged'])

    def reports(self,directory):
        ids=[f'test_fixture.Case.test_{i}' for i in range(9)]
        for index in range(4):
            chosen=shards.partition(ids,index,4)
            report={'schema_version':'backend-shard-v1','shard_index':index,'shard_count':4,
                'source_sha256':'fixture-source','source_unchanged':True,'all_test_ids':ids,
                'selected_ids':chosen,'executed_ids':chosen,'tests':len(chosen),
                'passed':True,'failures':[],'errors':[],'skipped':0}
            path=directory/str(index)/'backend-shard.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps(report),encoding='utf-8')
        return ids

    def test_coverage_requires_the_exact_complete_execution_set(self):
        with tempfile.TemporaryDirectory() as folder,redirect_stdout(io.StringIO()):
            root=Path(folder);ids=self.reports(root)
            result=shards.verify(root,4,root/'coverage.json')
            self.assertTrue(result['passed'])
            self.assertEqual(result['discovered'],len(ids))
            self.assertEqual(result['executed'],len(ids))
            self.assertEqual(result['unique_executed'],len(ids))

    def test_missing_duplicate_failed_or_inconsistent_shards_are_rejected(self):
        for variant in ('missing','duplicate','not_executed','different_source','failed','drift','different_discovery'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);self.reports(root);path=root/'0/backend-shard.json'
                report=json.loads(path.read_text(encoding='utf-8'))
                if variant=='missing':path.unlink()
                else:
                    if variant=='duplicate':report['shard_index']=1
                    elif variant=='not_executed':report['executed_ids']=report['executed_ids'][:-1]
                    elif variant=='different_source':report['source_sha256']='other-source'
                    elif variant=='failed':report['passed']=False
                    elif variant=='drift':report['source_unchanged']=False
                    elif variant=='different_discovery':report['all_test_ids']=['unrelated']
                    path.write_text(json.dumps(report),encoding='utf-8')
                with self.assertRaises(ValueError):shards.verify(root,4,root/'coverage.json')

    def test_release_command_keeps_full_default_and_binds_explicit_shard(self):
        output=Path('isolated-output').resolve()
        default,_=release.check_commands(output)['backend']
        self.assertIn('discover',default);self.assertNotIn('--shard-count',default)
        command,_=release.check_commands(output,backend_shard_index=2,backend_shard_count=4)['backend']
        self.assertIn('scripts/run_backend_tests.py',command)
        self.assertEqual(command[command.index('--shard-index')+1],'2')
        self.assertEqual(command[command.index('--shard-count')+1],'4')
        self.assertEqual(Path(command[-1]).name,'backend-shard.json')

    def test_successful_process_without_partition_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            _,result=release.execute_check('backend',[sys.executable,'-c','print("Ran 1 test in 0.0s")',
                '--shard-count','4'],root,root,'fixture',10)
            self.assertEqual(result['exit_code'],0)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(result['result_error'],'invalid_backend_shard_report')

    def test_valid_partition_evidence_is_accepted_including_one_test(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'backend-shard.json'
            report={'source_sha256':'fixture','source_unchanged':True,'passed':True,
                'shard_index':0,'shard_count':4,'all_test_ids':['a','b','c','d'],
                'selected_ids':['a'],'executed_ids':['a'],'tests':1,'skipped':0}
            code=(f'from pathlib import Path\nPath({str(output)!r}).write_text({json.dumps(report)!r},encoding="utf-8")\n'
                  'print("Ran 1 test in 0.0s")')
            _,result=release.execute_check('backend',[sys.executable,'-c',code,'--shard-count','4'],
                root,root,'fixture',10)
            self.assertEqual(result['status'],'passed')
            self.assertEqual(result['discovered_tests'],4)
            self.assertEqual(result['executed_tests'],1)
