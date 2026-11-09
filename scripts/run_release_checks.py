"""Shared offline release index; source drift invalidates a claimed baseline."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'scripts'))
from capture_upgrade_baseline import SOURCE_DIRS, capture

CHECK_NAMES=('backend','task_scenarios','frontend','build','read_only','architecture')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def check_commands(output,*,backend_shard_index=0,backend_shard_count=1):
    npm=['cmd.exe','/d','/c','npm.cmd'] if os.name=='nt' else ['npm']
    backend=[sys.executable,'-m','unittest','discover','-s','tests','-v']
    if backend_shard_count>1:
        backend=[sys.executable,'scripts/run_backend_tests.py','--shard-index',str(backend_shard_index),
            '--shard-count',str(backend_shard_count),'--output',str(output/'backend-shard.json')]
    return {
        'backend':(backend,ROOT),
        'task_scenarios':([sys.executable,'scripts/run_evaluations.py','evaluate_tasks','--output',str(output/'task-scenarios.json')],ROOT),
        'frontend':([*npm,'test','--','--run'],ROOT/'frontend'),
        'build':([*npm,'run','build','--','--outDir',str(output/'frontend-dist')],ROOT/'frontend'),
        'read_only':([sys.executable,'scripts/run_evaluations.py','evaluate_read_only','--source-manifest',str(output/'source.json'),
                      '--output',str(output/'read-only.json')],ROOT),
        'architecture':([sys.executable,'scripts/check_architecture.py'],ROOT),
    }


def execute_check(name,command,cwd,output,source_sha256,timeout):
    started=time.monotonic()
    env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
    log=output/(name+'.log')
    with log.open('w',encoding='utf-8') as stream:
        try:
            completed=subprocess.run(command,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
            code=completed.returncode
            status='passed' if code==0 else 'failed'
        except subprocess.TimeoutExpired:
            code,status=None,'timed_out'
            stream.write(f'\nCheck exceeded {timeout} seconds.\n')
        except OSError as error:
            code,status=None,'failed'
            stream.write('\nCould not start check: '+type(error).__name__+'\n')
    content=re.sub(r'\x1b\[[0-9;]*m','',log.read_text(encoding='utf-8',errors='replace'))
    count=re.search(r'Ran (\d+) tests?',content) if name=='backend' else re.search(r'Tests\s+(\d+) passed',content)
    details={'status':status,'exit_code':code,'elapsed_seconds':round(time.monotonic()-started,3),
        'log':log.name,'log_sha256':hashlib.sha256(log.read_bytes()).hexdigest(),
        'source_sha256':source_sha256,'tests':int(count[1]) if count else None}
    if name=='backend':
        details['failures']=[owner+'.'+method for method,owner in re.findall(
            r'^(?:FAIL|ERROR): (test_\w+) \(([\w.]+)\)',content,re.M)]
    if name=='backend' and '--shard-count' in command:
        try:
            shard=json.loads((output/'backend-shard.json').read_text(encoding='utf-8'))
            if (shard['source_sha256']!=source_sha256 or not shard['source_unchanged']
                    or shard['executed_ids']!=shard['selected_ids'] or shard['tests']!=details['tests']):
                raise ValueError('Invalid backend partition evidence')
            details.update(shard_index=shard['shard_index'],shard_count=shard['shard_count'],
                discovered_tests=len(shard['all_test_ids']),executed_tests=len(shard['executed_ids']),skipped=shard['skipped'])
            if not shard['passed']:details['status']='failed'
        except (OSError,ValueError,KeyError,TypeError):
            details.update(status='failed',result_error='invalid_backend_shard_report')
    result_file={'task_scenarios':'task-scenarios.json','read_only':'read-only.json'}.get(name)
    if result_file and (output/result_file).exists():
        try:
            raw=json.loads((output/result_file).read_text(encoding='utf-8'))
            details.update(corpus_sha256=raw['corpus_sha256'],summary=raw.get('summary'))
        except (ValueError,KeyError):
            details.update(status='failed',result_error='invalid_report')
    return name,details


def run(output,checks=None,timeout=1800,*,backend_shard_index=0,backend_shard_count=1):
    selected=tuple(checks) if checks is not None else CHECK_NAMES
    if not selected or len(set(selected))!=len(selected) or set(selected)-set(CHECK_NAMES):
        raise ValueError('Choose distinct supported checks.')
    if timeout<=0:
        raise ValueError('Check timeout must be positive.')
    if backend_shard_count<1 or not 0<=backend_shard_index<backend_shard_count:
        raise ValueError('Invalid backend shard index or count.')
    if 'backend' not in selected and (backend_shard_index or backend_shard_count!=1):
        raise ValueError('Backend shard options require the backend check.')
    output=output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new output directory; previous evidence is immutable.')
    if output==ROOT or any(output.is_relative_to(ROOT/folder) for folder in (*SOURCE_DIRS,'frontend','evals','data')):
        raise ValueError('Release outputs must be outside the captured source/corpus folders.')
    output.mkdir(parents=True,exist_ok=True)
    source=capture()
    (output/'source.json').write_text(json.dumps(source,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with zipfile.ZipFile(output/'source.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
        for row in source['source_files']:
            path=ROOT/row['path']
            if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
                raise ValueError('Source drift during archive capture.')
            archive.write(path,row['path'])
    jobs=check_commands(output,backend_shard_index=backend_shard_index,backend_shard_count=backend_shard_count)
    report={'schema_version':'release-checks-v2','started_at':stamp(),'source_sha256':source['source_sha256'],
            'head':source['head'],'mode':'offline','model_config':{'test_models':'scripted/mocked','external_usage':None,
            'external_cost':None,'note':'No real model/API probes requested; this is not a real-model quality measurement.'},
            'selected_checks':selected,'complete_suite':set(selected)==set(CHECK_NAMES),'checks':{}}
    report['source_manifest']='source.json'
    report['source_archive']={'path':'source.zip','sha256':hashlib.sha256((output/'source.zip').read_bytes()).hexdigest()}
    report['prompt_skill_manifest']=[row for row in source['source_files'] if row['path'].startswith(('skills/','prompts/'))]
    from infrastructure.skill_manager import get_skill_manager
    report['skill_registry']=get_skill_manager().status()
    report['corpus_manifest']=[row for row in source['source_files'] if row['path'].startswith('tests/fixtures/') or
        (row['path'].startswith('evals/') and '_cases_' in row['path'])]
    def save():
        target=output/'index.json'
        temporary=output/'index.tmp'
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(target)
    save()
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending=[pool.submit(execute_check,name,*jobs[name],output,source['source_sha256'],timeout) for name in selected]
        for future in as_completed(pending):
            name,details=future.result()
            report['checks'][name]=details
            save()
            print(json.dumps({'check':name,**details}),flush=True)
    final=capture()
    report.update(finished_at=stamp(),source_sha256_at_finish=final['source_sha256'],
                  source_unchanged=final['source_sha256']==source['source_sha256'])
    report['passed']=(report['source_unchanged'] and set(report['checks'])==set(selected)
                      and all(c['status']=='passed' for c in report['checks'].values()))
    save()
    print(json.dumps({'passed':report['passed'],'source_unchanged':report['source_unchanged']}),flush=True)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--checks',nargs='+',choices=CHECK_NAMES)
    parser.add_argument('--timeout',type=float,default=1800,help='Maximum seconds per check')
    parser.add_argument('--backend-shard-index',type=int,default=0)
    parser.add_argument('--backend-shard-count',type=int,default=1)
    args=parser.parse_args()
    raise SystemExit(0 if run(args.output,args.checks,args.timeout,backend_shard_index=args.backend_shard_index,backend_shard_count=args.backend_shard_count) else 1)
