"""Validate previously captured responses offline. Does not regenerate summaries."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.summary_contract import PROMPT_VERSION, SummaryValidationError, load_summary_prompt
from services.conversation_summary_service import validate_items
from core.summary_semantics import SEMANTICS_VERSION


def replay(path):
    captured = json.loads(path.read_text(encoding='utf-8'))
    results = []
    for number, call in enumerate(captured['physical_requests'], 1):
        if call['purpose'] != 'conversation_summary':
            continue
        payload = json.loads(call['request']['messages'][-1]['content'])
        value = json.loads(call['content'])
        failures = []
        for index, item in enumerate(value['items']):
            try:
                validate_items({'items': [item]}, payload)
            except SummaryValidationError as error:
                failures.append({'item': index + 1, **error.details})
        try:
            validate_items(value, payload)
            accepted = True
        except SummaryValidationError:
            accepted = False
        results.append({'physical_call': number, 'event': call['event_key'],
                        'full_output_accepted': accepted, 'items': len(value['items']), 'failures': failures})
    return {'mode': 'offline-captured-response-replay',
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'validator_version': SEMANTICS_VERSION, 'prompt_version': PROMPT_VERSION,
            'source_prompt_version': captured.get('summary_prompt_version', 'legacy-unspecified'),
            'prompt_sha256': load_summary_prompt()[1],
            'real_model_calls': 0, 'results': results,
            'note': 'Captured output replay tests validators only, NOT newly generated recall or model quality. '
                    'Source-count violations remain rejected; missing facts cannot be recovered by a validator.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'tests/fixtures/summary_captured_responses_v3.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = replay(args.input)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
