"""Read only reviewed public policy pages; no models, Tavily or Amap calls."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.attraction_facts import page_facts, reconcile_facts
from infrastructure.web_reader import read_page
from services.official_sources import OfficialSources, scoped_url


def main(output):
    registry = OfficialSources()
    report = {'mode': 'reviewed-public-policy-pages', 'model_calls': 0, 'tavily_calls': 0, 'amap_calls': 0, 'results': []}
    for index, entry in enumerate(registry.entries):
        started = time.monotonic()
        result = {'entity': entry['canonical_name'], 'url': entry['policy_urls'][0]}
        try:
            source = read_page(result['url'], url_allowed=lambda url: scoped_url(url, entry))
            source.update(id=index + 1, official_identity=registry.verify(entry['canonical_name'], source['url'], entry['city']))
            target = {'name': entry['canonical_name'], 'visit_mode': 'entry'}
            facts = reconcile_facts(page_facts(source, target, f'target-{index + 1}',
                ['opening_hours', 'ticket_price', 'reservation_rules'], '2026-11-03', entry))
            result.update(status='read', final_url=source['url'], retrieved_at=source['retrieved_at'],
                content_hash=source['content_hash'], identity=source['official_identity'], facts=facts,
                topic_coverage={topic: sorted({f['status'] for f in facts if f['topic'] == topic})
                    for topic in ('opening_hours', 'ticket_price', 'reservation_rules')})
        except Exception as error:
            result.update(status='unavailable', error_type=type(error).__name__, error_code=getattr(error, 'code', ''))
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['results'].append(result)
        print(json.dumps({'entity': result['entity'], 'status': result['status'], 'elapsed_seconds': result['elapsed_seconds']}, ensure_ascii=False), flush=True)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    report['note'] = 'Exact quotes and identity scopes, not an LLM quality score. Pending dates and unavailable topics remain in the denominator.'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args().output)
