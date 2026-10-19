"""Record final regression evidence with tool/version metadata, not test payloads."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import os
import re
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def source_snapshot(folders=('adapters','agents','app','core','infrastructure','services','tools','tests'), pattern='*.py'):
    source=[]
    for folder in folders:
        for path in sorted((ROOT/folder).rglob(pattern)):
            source.append((path.relative_to(ROOT).as_posix(),hashlib.sha256(path.read_bytes()).hexdigest()))
    return source


def main(output):
    before=source_snapshot()
    started=time.monotonic()
    result=subprocess.run([sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-q'],
                          cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace')
    match=re.search(r'Ran (\d+) tests in ([\d.]+)s',result.stderr)
    source=source_snapshot()
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'python':platform.python_version(),
            'python_executable':sys.executable,'passed':result.returncode==0 and before==source,'exit_code':result.returncode,
            'source_unchanged':before==source,
            'tests':int(match[1]) if match else None,'elapsed_seconds':round(time.monotonic()-started,3),
            'source_hash':hashlib.sha256(json.dumps(source).encode()).hexdigest(),'source_files':source,
            'failures':re.findall(r'^(?:FAIL|ERROR): (.+)$',result.stderr,re.M),
            'note':'全量后端回归；故障注入日志不代表正式服务故障。未使用正式数据库或付费API。'}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('python','passed','tests','elapsed_seconds','failures')},ensure_ascii=False),flush=True)
    raise SystemExit(0 if report['passed'] else 1)


def frontend(output,out_dir=None):
    before=source_snapshot(('frontend/src',),'*')
    started=time.monotonic()
    npm='npm.cmd' if os.name=='nt' else 'npm'
    tested=subprocess.run([npm,'test','--','--run'],cwd=ROOT/'frontend',capture_output=True,text=True,encoding='utf-8',errors='replace')
    build_command=[npm,'run','build']+(['--','--outDir',str(out_dir)] if out_dir else [])
    built=subprocess.run(build_command,cwd=ROOT/'frontend',capture_output=True,text=True,encoding='utf-8',errors='replace')
    raw=re.sub(r'\x1b\[[0-9;]*m','',tested.stdout)
    match=re.search(r'Tests\s+(\d+) passed\s+\((\d+)\)',raw)
    after=source_snapshot(('frontend/src',),'*')
    passed=tested.returncode==0 and built.returncode==0 and before==after and bool(match) and match[1]==match[2]
    report={'mode':'frontend-final','passed':passed,'tests':int(match[2]) if match else None,
            'tests_exit_code':tested.returncode,'build_exit_code':built.returncode,'source_unchanged':before==after,
            'elapsed_seconds':round(time.monotonic()-started,3),'source_files':after,
            'generated_at':datetime.now(timezone.utc).isoformat(),'build_out_dir':str(out_dir) if out_dir else 'frontend/dist'}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('mode','passed','tests','build_exit_code')}))
    raise SystemExit(0 if passed else 1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--frontend',action='store_true')
    parser.add_argument('--frontend-out-dir',type=Path)
    args=parser.parse_args()
    if args.frontend:
        frontend(args.output,args.frontend_out_dir)
    else:
        main(args.output)
