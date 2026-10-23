"""At most one final-writing submission per event, persisted before publishing."""
from datetime import datetime, timezone
import time

from core.context_contract import response_json
from core.execution_scope import CURRENT_EXECUTION, ExecutionRevoked, ensure_execution_active
from core.model_budget import model_call_context
from core.model_policy import output_limit_for_purpose, model_for_purpose
from core.travel_answer import VERSION, fingerprint, limits, presentation, render, writing_messages, validate_written


def remaining_seconds(store):
    ensure_execution_active()
    scope = CURRENT_EXECUTION.get()
    if scope is None:
        return 100.0
    with store._connect() as db:
        row = db.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?',(scope.job_id,)).fetchone()
    return (datetime.fromisoformat(row['deadline_at'])-datetime.now(timezone.utc)).total_seconds()


def write_answer(service, event, data):
    base = presentation(data)
    metadata = {'schema_version':VERSION, 'prompt_version':VERSION, 'model':model_for_purpose(service.model,'travel_answer_write'),
                'source_hash':fingerprint([(s.get('content_hash'),s.get('text')) for s in data.get('sources',[])]),
                'snapshot':{k:data.get(k) for k in ('dates','provider_dates','target_scope','plan_title','plan_version','plan_snapshot')},
                'request':event.content, 'started_at':datetime.now(timezone.utc).isoformat()}
    record, acquired = service.store.research.reserve_answer_write(event, metadata)
    if not acquired:
        if record.get('result'):
            data['answer_write'] = record
            return render(record['result'])
        # Reserved/unknown calls never get a second submission after recovery.
        return render(base)
    data['answer_write'] = record
    started, submitted, received = time.monotonic(), False, False
    try:
        _, timeout_limit = limits()
        timeout = min(timeout_limit, remaining_seconds(service.store)-10)
        if service.client is None:
            raise ValueError('model_unconfigured')
        if timeout < 5:
            raise ValueError('insufficient_time')
        messages, supplied = writing_messages(base, event.content, metadata['snapshot'])
        from infrastructure.model_gateway import estimate_input
        record['input_estimate'] = estimate_input(messages)
        with model_call_context('travel_answer_write', prompt_version=VERSION, context_policy_version=VERSION):
            submitted = True
            response = service.client.chat.completions.create(model=service.model, response_format={'type':'json_object'},
                messages=messages, max_tokens=output_limit_for_purpose('travel_answer_write',8192), timeout=timeout)
        received = True
        usage = getattr(response,'usage',None)
        record['usage'] = {key:getattr(usage,key,None) for key in ('prompt_tokens','completion_tokens','total_tokens')}
        result = validate_written(response_json(response), base, supplied)
        record['status'] = 'completed'
    except ExecutionRevoked:
        raise
    except Exception as error:
        result = base
        from core.model_budget import ModelBudgetExceeded
        record.update(status='unknown' if submitted and not received and not isinstance(error,ModelBudgetExceeded) else 'fallback', error_type=type(error).__name__)
        result['degradations'].append(str(error) if isinstance(error,ValueError) and not submitted else type(error).__name__)
    ensure_execution_active()
    record.update(elapsed_ms=round((time.monotonic()-started)*1000),
                  finished_at=datetime.now(timezone.utc).isoformat())
    result['diagnostics']['writing'] = {key:record.get(key) for key in ('status','model','input_estimate','usage','elapsed_ms','error_type')}
    result['diagnostics']['writing'].update(generation=result['generation'],degradations=result['degradations'])
    record['result'] = result
    data.update(answer_write=record, brief_report=render(result), report=render(result,detailed=True), answer_schema=VERSION)
    service.store.research.save(event, data.get('stage','partial'), data)
    return data['brief_report']
