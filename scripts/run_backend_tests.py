"""Run complete deterministic unittest partitions and verify their combined coverage."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def partition(ids,index,count):
    if count<1 or not 0<=index<count:
        raise ValueError('Choose a valid shard index and positive shard count.')
    if len(ids)!=len(set(ids)):
        raise ValueError('Discovery contains duplicate test IDs.')
    return sorted(ids)[index::count]


def discover():
    loader=unittest.TestLoader()
    suite=loader.discover(str(ROOT/'tests'),top_level_dir=str(ROOT/'tests'))
    if loader.errors:
        raise ValueError('Backend discovery failed; no partial suite will be accepted.')
    def cases(item):
        if isinstance(item,unittest.TestSuite):
            for child in item:yield from cases(child)
        else:yield item
    return list(cases(suite))


def source_identity():
    from scripts.capture_upgrade_baseline import capture
    return capture()['source_sha256']


class RecordedResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.executed_ids=[]

    def startTest(self,test):
        self.executed_ids.append(test.id())
        super().startTest(test)


def run(output,index=0,count=1):
    if output.exists():raise ValueError('Use a new shard report path.')
    before=source_identity()
    cases=discover();ids=sorted(case.id() for case in cases)
    if not ids:raise ValueError('Backend discovery found no tests.')
    selected=partition(ids,index,count);by_id={case.id():case for case in cases}
    result=unittest.TextTestRunner(verbosity=2,resultclass=RecordedResult).run(
        unittest.TestSuite(by_id[identity] for identity in selected))
    unchanged=source_identity()==before
    report={'schema_version':'backend-shard-v1','shard_index':index,'shard_count':count,
        'source_sha256':before,'source_unchanged':unchanged,'all_test_ids':ids,
        'selected_ids':selected,'executed_ids':result.executed_ids,'tests':result.testsRun,
        'failures':[case.id() for case,_ in result.failures],
        'errors':[case.id() for case,_ in result.errors],'skipped':len(result.skipped)}
    report['passed']=result.wasSuccessful() and unchanged and result.executed_ids==selected
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('shard_index','shard_count','passed','tests','skipped','source_unchanged')},ensure_ascii=False),flush=True)
    return report


def verify(directory,count,output):
    if output.exists():raise ValueError('Use a new coverage report path.')
    reports=[json.loads(path.read_text(encoding='utf-8')) for path in sorted(directory.rglob('backend-shard.json'))]
    if count<1 or len(reports)!=count:
        raise ValueError('Missing backend shard reports.')
    expected=reports[0]['all_test_ids'];source=reports[0]['source_sha256']
    if not expected or len(expected)!=len(set(expected)):
        raise ValueError('Invalid complete discovery list.')
    indices=set();executed=[]
    for report in reports:
        index=report['shard_index']
        if (report['schema_version']!='backend-shard-v1' or report['shard_count']!=count
                or index in indices or not 0<=index<count or report['all_test_ids']!=expected
                or report['source_sha256']!=source or report['source_unchanged'] is not True
                or report['passed'] is not True or report['failures'] or report['errors']):
            raise ValueError('Inconsistent or failed backend shard.')
        selected=partition(expected,index,count)
        if (report['selected_ids']!=selected or report['executed_ids']!=selected
                or report['tests']!=len(selected)):
            raise ValueError('Backend shard execution differs from its discovery partition.')
        indices.add(index);executed.extend(report['executed_ids'])
    if indices!=set(range(count)) or Counter(executed)!=Counter(expected):
        raise ValueError('Backend coverage has missing or duplicate test IDs.')
    result={'passed':True,'shard_count':count,'source_sha256':source,
        'discovered':len(expected),'executed':len(executed),'unique_executed':len(set(executed)),
        'skipped':sum(report['skipped'] for report in reports)}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--shard-index',type=int,default=0)
    parser.add_argument('--shard-count',type=int,default=1)
    parser.add_argument('--verify',type=Path)
    args=parser.parse_args()
    try:
        result=(verify(args.verify,args.shard_count,args.output) if args.verify else
                run(args.output,args.shard_index,args.shard_count))
    except (ValueError,KeyError,TypeError) as error:
        print(json.dumps({'passed':False,'error':str(error)}),flush=True)
        raise SystemExit(1)
    raise SystemExit(0 if result['passed'] else 1)
