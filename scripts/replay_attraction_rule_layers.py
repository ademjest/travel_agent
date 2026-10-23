"""Read-only local replay of saved research; no models, Amap, web or DB writes."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.attraction_projection import attraction_report, grouped_facts, planning_facts


def replay(database):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute("SELECT data_json FROM research_runs WHERE json_extract(data_json,'$.fact_schema')='attraction-facts-v1' "
                         "ORDER BY length(json_extract(data_json,'$.report')) DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError('No saved structured attraction research to replay')
    data = json.loads(row[0])
    original = deepcopy(data)
    report = attraction_report(data, detailed=True)
    brief = attraction_report(data)
    sections = report.split('#### 普通入馆相关条款\n')[1:]
    primary = '\n'.join(section.split('\n#### ', 1)[0] for section in sections)
    planning = planning_facts(data)
    payload = json.loads(planning.split('\n', 1)[1])
    sources = {s['id']: s for s in data.get('sources', [])}
    groups = grouped_facts(data['facts'], sources,
                           {f'target-{i + 1}': t['name'] for i, t in enumerate(data['target_scope']['targets'])})
    result = {'mode': 'local_readonly_rule_layers_replay', 'external_calls': 0,
              'brief_chars': len(brief), 'complete_report_chars': len(report), 'ordinary_section_chars': len(primary),
              'original_facts': len(data['facts']), 'display_groups': len(groups),
              'buckets': {key: sum(g['rule_scope']['bucket'] == key for g in groups)
                          for key in ('current', 'platform', 'variants', 'reference')},
              'date_pending_phrase_occurrences': report.count('目标日期/例外待确认'),
              'generic_ticket_boilerplate_occurrences': report.count('一般入馆，条件见原文'),
              'planning_chars': len(planning), 'planning_target_ids': sorted({f['target_id'] for f in payload['facts']}),
              'checks': {'input_unchanged': original == data, 'brief_bounded': len(brief) <= 6000,
                         'planning_bounded': len(planning) <= 6000,
                         'no_special_addon_in_ordinary_section': not re.search(r'珍宝馆门票|钟表馆门票|公园游览年票', primary),
                         'no_past_occupation_event_in_ordinary_section': not re.search(r'环卫工人|公共交通司乘人员', primary),
                         'reference_facts_excluded_from_planning': not any(
                             f.get('rule_scope', {}).get('bucket') == 'reference' for f in payload['facts']),
                         'separate_evidence_section': '#### 原始证据与核验细节' in report}}
    result['passed'] = all(result['checks'].values())
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'data/web/travel_bot.db')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        raise ValueError('Refusing to overwrite existing replay audit')
    result = replay(args.database)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
