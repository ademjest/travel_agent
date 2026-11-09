"""Probe Wendao's prompt-only JSON contract; preview by default, one submission with --live."""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VERSION = 'wendao-flat-v2'
PLACES = ('六朝博物馆', '总统府', '江宁织造博物馆', '古鸡鸣寺',
          '南京城墙景区(神策门至太平门)', '玄武湖景区', '南京博物院', '白马公园',
          '九华山公园', '南京地质博物馆', '北极阁公园', '环洲')


def build_request(case, start):
    names = PLACES[:1] if case == 'single' else (PLACES[0], PLACES[3], PLACES[5]) if case == 'small' else PLACES
    targets = [{'target_id': f'target-{i+1}', 'name': name,
                'visit_date': (start + timedelta(days=i//3)).isoformat()}
               for i, name in enumerate(names)]
    example = {'name': names[0], 'introduction': None, 'opening_hours': None,
               'adult_ticket_price': None, 'reservation': None}
    if case == 'itinerary':
        travel = f'我计划从{start.year}年{start.month}月{start.day}日起在南京游玩4天。'
        for i in range(0, len(targets), 3):
            travel += f'第{i//3+1}天（{targets[i]["visit_date"]}）：' + '、'.join(t['name'] for t in targets[i:i+3]) + '。'
    else:
        travel = f'我计划{start.year}年{start.month}月{start.day}日在南京游览' + '、'.join(names) + '。'
    query = (travel + '请分别介绍这些景点的历史文化特色，并查询各自游览日的开放时间、普通成人票价和预约方式。'
        '查不到的信息请填null，不要推测。成人票价填人民币数字，免费为0。'
        '为方便我的旅行软件展示，请' + ('只返回一个JSON对象' if case == 'single' else '返回JSON数组，每个景点一个对象，name保留景点原名')
        + '，字段与下面示例一致，不要代码围栏或额外说明：'
        + json.dumps(example if case == 'single' else [example], ensure_ascii=False))
    return targets, query


def strict_json(text):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('nonfinite_json_number')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def decode_response(text):
    """Separate the known transport envelope from a direct business JSON object."""
    def payload(raw):
        fence = re.fullmatch(r'\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*', raw, re.S | re.I)
        return strict_json(fence[1] if fence else raw), bool(fence)
    value, fenced = payload(text)
    if isinstance(value, dict) and 'result' in value and 'schema_version' not in value:
        if value.get('error'):
            raise ValueError('provider_business_error')
        content = value['result']
        if isinstance(content, dict):
            content = content.get('content')
        value, inner_fenced = payload(content)
        return value, 'result_envelope' + ('/json_code_fence' if fenced or inner_fenced else '')
    return value, 'json_code_fence' if fenced else 'direct_json'


def validate_contract(value, targets, case='small'):
    if case == 'single':
        if not isinstance(value, dict):
            return {'contract_pass': False, 'errors': ['$:object']}
        entries = [value]
    else:
        if not isinstance(value, list):
            return {'contract_pass': False, 'errors': ['$:array']}
        entries = value
    errors, adjustments, seen = [], [], []
    intro_count, price_count = 0, 0
    expected = {t['name'] for t in targets}
    fields = {'name', 'introduction', 'opening_hours', 'adult_ticket_price', 'reservation'}
    for i, entry in enumerate(entries):
        path = f'entries[{i}]'
        if not isinstance(entry, dict) or set(entry) != fields:
            errors.append(path + ':fields')
            continue
        name = entry['name']
        if isinstance(name, str) and (wrapped := re.fullmatch(r'<([^<>]+)>', name)):
            name = wrapped[1]
            if 'name_angle_brackets' not in adjustments:
                adjustments.append('name_angle_brackets')
        if not isinstance(name, str) or name not in expected:
            errors.append(path + ':name_mismatch')
            continue
        seen.append(name)
        for field in ('introduction', 'opening_hours', 'reservation'):
            item = entry[field]
            if item is not None and not (isinstance(item, str) and item.strip()):
                errors.append(path + '.' + field + ':text')
        intro_count += isinstance(entry['introduction'], str) and bool(entry['introduction'].strip())
        amount = entry['adult_ticket_price']
        if amount is not None:
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                errors.append(path + '.adult_ticket_price:amount')
            else:
                price_count += 1
    if len(seen) != len(targets) or set(seen) != set(expected) or len(set(seen)) != len(seen):
        errors.append('entries:missing_or_duplicate_targets')
    return {'contract_pass': not errors, 'errors': errors, 'received_count': len(entries),
            'introduction_count': intro_count, 'known_price_target_count': price_count,
            'format_adjustments': adjustments, 'factual_accuracy': 'not_independently_verified',
            'date_applicability': 'not_checked_by_flat_protocol'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true', help='Send exactly one paid/usage-counted request; no retry')
    mode.add_argument('--response', type=Path, help='Validate a saved raw response offline')
    parser.add_argument('--case', choices=('single', 'small', 'itinerary'), default='small')
    parser.add_argument('--start-date', type=date.fromisoformat, help='Default: next Saturday in Beijing time')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--output', type=Path, help='New evidence directory; an existing directory is refused')
    args = parser.parse_args(argv)
    now = datetime.now(timezone(timedelta(hours=8)))
    start = args.start_date or now.date() + timedelta(days=(5-now.weekday()) % 7 or 7)
    targets, query = build_request(args.case, start)
    if not args.live and args.response is None:
        print(query)
        return 0
    output = args.output or ROOT / '.upgrade-testing' / 'wendao-contract' / (now.strftime('%Y%m%dT%H%M%S%f') + '-' + args.case)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'query.txt').write_text(query, encoding='utf-8')
    report = {'case': args.case, 'protocol_version': VERSION, 'targets': targets, 'started_at': now.isoformat(),
              'response_source': str(args.response.resolve()) if args.response else None,
              'mode': 'live' if args.live else 'offline', 'submission_attempts': 0,
              'max_submissions': 1, 'retries': 0, 'cost': None, 'usage': None,
              'status': 'prepared', 'contract_pass': None, 'strict_contract_pass': None}
    def save():
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    started = time.monotonic()
    try:
        if args.live:
            from dotenv import dotenv_values
            from infrastructure.public_http import request_public
            from infrastructure.wendao_client import ENDPOINT, MAX_BYTES, WendaoClient
            token = os.getenv('WENDAO_API_KEY', '').strip()
            if not token:
                token = (dotenv_values(args.env_file).get('WENDAO_API_KEY') or '').strip()
            if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError('missing_or_invalid_wendao_token')
            report.update(status='submitted', submission_attempts=1)
            save()  # A timeout/interruption must not silently cause another submission.
            print('Submitting one Wendao request; no retries.', flush=True)
            body, _, mime = request_public(ENDPOINT, payload={'token': token, 'query': query},
                max_bytes=MAX_BYTES, timeout=90, redirects=0, max_attempts=1, persist=False,
                content_types=('text/markdown', 'text/plain', 'application/json'))
            raw = WendaoClient(token).redact(body.decode('utf-8-sig'))
            report.update(status='received', mime=mime, response_bytes=len(body))
        else:
            raw = args.response.read_text(encoding='utf-8-sig')
            report['status'] = 'received'
        (output / 'response.txt').write_text(raw, encoding='utf-8')
        report['contract_pass'] = False
        report['strict_contract_pass'] = False
        value, envelope = decode_response(raw)
        report.update(envelope=envelope, **validate_contract(value, targets, args.case))
        adjustments = report.setdefault('format_adjustments', [])
        if 'json_code_fence' in envelope:
            adjustments.append('json_code_fence')
        report['strict_contract_pass'] = report['contract_pass'] and not adjustments
        (output / 'parsed.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        report['status'] = 'validated'
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__,
                      error_code=getattr(error, 'code', '') or ('invalid_json' if isinstance(error, json.JSONDecodeError) else 'probe_failed'))
        if isinstance(error, PermissionError) and not report['submission_attempts']:
            report.update(status='blocked', error_code='file_access_denied')
        elif isinstance(error, ValueError) and str(error) == 'missing_or_invalid_wendao_token':
            report.update(status='blocked', error_code='missing_or_invalid_wendao_token')
    report['elapsed_seconds'] = round(time.monotonic()-started, 3)
    save()
    print(json.dumps({'report': str(output / 'report.json'), **{k: report[k] for k in
        ('status', 'submission_attempts', 'contract_pass', 'strict_contract_pass', 'elapsed_seconds')}}, ensure_ascii=False))
    return 0 if report['contract_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
