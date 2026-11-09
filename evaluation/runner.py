"""One evaluation command with explicit execution modes and versioned result envelopes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import runpy
import sys
import time

from evaluation.scenarios import ALIASES, SCENARIOS
from evaluation.support import ROOT, reported_usage

MODES = ('offline', 'real_model_fixed_tools', 'live_provider')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=MODES, default='offline')
    parser.add_argument('--token-budget', type=int)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('scenario', nargs='?')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps({name: {'modes': row.modes, 'description': row.description}
            for name, row in SCENARIOS.items()}, ensure_ascii=False, indent=2))
        return 0
    name = ALIASES.get(args.scenario, args.scenario)
    if name not in SCENARIOS:
        parser.error('Choose a scenario from --list.')
    scenario = SCENARIOS[name]
    if args.mode not in scenario.modes:
        parser.error('This scenario does not support the selected mode; no requests were sent.')
    forwarded = args.arguments
    if forwarded and forwarded[0] == '--':
        forwarded = forwarded[1:]
    if args.mode == 'offline' and any(flag in forwarded for flag in ('--live', '--live-model', '--napcat', '--allow-paid', '--authorize-live', '--capture', '--continue-pages', '--live-providers')):
        parser.error('Offline mode cannot enable provider/model calls through scenario flags.')
    if args.mode != 'offline':
        if name != 'rail_mcp' and (not args.token_budget or not 1 <= args.token_budget <= 100000):
            parser.error('Real execution requires an explicit positive token budget within the safety ceiling.')
        # Every real recipe must bind the shared persisted budget before it can run.
        from evaluation.support import real_execution
        context = real_execution(name, args.token_budget, forwarded, args.report)
    else:
        from evaluation.support import offline_execution
        context = offline_execution()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report_path = args.report or ROOT/'.upgrade-testing/evaluation'/stamp/(name+'.json')
    if report_path.exists():
        raise FileExistsError('Previous evaluation evidence cannot be overwritten.')
    from scripts.capture_upgrade_baseline import capture
    source = capture()
    report = {'schema_version': 'evaluation-result-v1', 'scenario': name, 'mode': args.mode,
        'source_sha256': source['source_sha256'], 'started_at': datetime.now(timezone.utc).isoformat(),
        'prompt_manifest': [row for row in source['source_files'] if row['path'].startswith(('skills/', 'prompts/'))],
        'corpus_manifest': [row for row in source['source_files'] if row['path'].startswith('evals/')],
        'actual_usage': None, 'actual_cost': None, 'task_metrics': None, 'failure_slices': [],
        'note': 'Execution status is separate from task success; scripted calls are not real-model quality scores.'}
    started = time.monotonic()
    previous = sys.argv
    code, failure = 0, ''
    execution = None
    try:
        sys.argv = [scenario.module, *forwarded]
        with context as execution:
            runpy.run_module(scenario.module, run_name='__main__')
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else int(bool(exc.code))
    except Exception as exc:
        code, failure = 1, type(exc).__name__
        raise
    finally:
        sys.argv = previous
        report.update(exit_code=code, execution_status='passed' if code == 0 else 'failed', failure_type=failure,
            elapsed_seconds=round(time.monotonic()-started, 6),
            finished_at=datetime.now(timezone.utc).isoformat(), source_unchanged=capture()['source_sha256']==source['source_sha256'])
        if execution is not None:
            report['actual_usage'] = reported_usage(execution.records)
            report['physical_requests_audit'] = str(execution.output)
        if '--output' in forwarded:
            native_path = Path(forwarded[forwarded.index('--output')+1])
            if native_path.is_file():
                report['native_result'] = str(native_path.resolve())
                report['native_sha256'] = hashlib.sha256(native_path.read_bytes()).hexdigest()
                try:
                    native = json.loads(native_path.read_text(encoding='utf-8'))
                    report['task_metrics'] = native.get('summary') if isinstance(native, dict) else None
                    if isinstance(native, dict) and name == 'evaluate_summary_quality':
                        report['task_metrics'] = {key: native[key] for key in ('mechanism_passed', 'known_guard_failures')}
                    if isinstance(native, dict):
                        report['failure_slices'] = [row for row in native.get('results', [])
                            if isinstance(row, dict) and (row.get('passed') is False or row.get('mechanism_passed') is False)]
                        if args.mode != 'offline' and execution is None and 'physical_requests' in native:
                            report['actual_usage'] = reported_usage(native['physical_requests'])
                except ValueError:
                    report['native_result_error'] = 'invalid_json'
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
