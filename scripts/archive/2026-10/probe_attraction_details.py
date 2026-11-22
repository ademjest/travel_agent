"""Explicitly authorized, at most ten single-attempt POI 2.0 requests. No LLM/search."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from core.web_settings import WebSettings
from infrastructure.amap_client import AmapClient

TARGETS = [('武汉', name) for name in ('湖北省博物馆', '黄鹤楼', '东湖磨山景区', '武汉园博园', '武汉动物园')]
TARGETS += [('北京', name) for name in ('故宫博物院', '中国国家博物馆', '颐和园')]
TARGETS += [('杭州', '西湖风景名胜区'), ('苏州', '拙政园')]


def main(output):
    if output.exists():
        raise ValueError('Audit already exists; refusing to silently spend another ten calls.')
    settings = WebSettings.from_env().travel_settings()
    report = {'mode': 'authorized-amap-v5-only', 'max_physical_calls': 10, 'physical_calls': 0,
              'model_calls': 0, 'tavily_calls': 0, 'retries': 0, 'results': []}
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    with requests.Session() as session:
        session.trust_env = False

        def get(url, **kwargs):
            if report['physical_calls'] >= 10:
                raise ValueError('Authorized physical call limit reached')
            report['physical_calls'] += 1
            save()
            response = session.get(url, allow_redirects=False, **kwargs)
            if 300 <= response.status_code < 400:
                raise requests.HTTPError('Redirect refused')
            return response

        client = AmapClient(settings.amap_api_key, http_get=get, max_attempts=1)
        for city, name in TARGETS:
            started = time.monotonic()
            result = {'city': city, 'name': name, 'started_at': datetime.now(timezone.utc).isoformat()}
            try:
                result.update(status='completed', candidates=client.search_poi_details(city, name))
            except Exception as error:
                result.update(status='failed', error_type=type(error).__name__, error_code=getattr(error, 'code', ''))
            result['elapsed_seconds'] = round(time.monotonic() - started, 3)
            report['results'].append(result)
            save()
            print(json.dumps({key: result[key] for key in ('city', 'name', 'status', 'elapsed_seconds')}, ensure_ascii=False), flush=True)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({'physical_calls': report['physical_calls'], 'model_calls': 0, 'tavily_calls': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args().output)
