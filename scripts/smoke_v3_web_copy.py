"""Web health smoke against a migrated COPY, with all workers and external services off."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from adapters.web_app import create_web_app
from core.settings import Settings
from core.web_settings import WebSettings
from fastapi.testclient import TestClient


def main(directory,output):
    directory=directory.resolve()
    allowed=(ROOT/'data/upgrades').resolve()
    if not directory.is_relative_to(allowed) or not (directory/'travel_bot.db').is_file():
        raise ValueError('冒烟仅允许data/upgrades中的迁移副本，拒绝正式数据库。')
    app=create_web_app(WebSettings(data_dir=directory,open_browser=False),
        Settings('','',frozenset(),'','','',''),start_workers=False,
        settings_store=SimpleNamespace(status=lambda:{'version':'copy-only','fields':{},'restart_required':False}))
    with TestClient(app,base_url='http://127.0.0.1:8080') as client:
        statuses={path:client.get(path).status_code for path in ('/health/live','/health/ready','/')}
    report={'mode':'web-copy-smoke','passed':all(value==200 for value in statuses.values()),
            'statuses':statuses,'network_calls':0,'start_workers':False,'formal_service':False,
            'note':'仅验证迁移副本可装配和返回健康/静态资源，不代表正式模型或提醒上线验收。'}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    if not report['passed']:
        raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    main(args.directory,args.output)
