"""Shared evaluation budgets and transports. No environment-file or database access on import."""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import httpx
from core.model_budget import MODEL_CALL, MODEL_EVENT, MODEL_ROUTE,ModelBudgetExceeded
from core.model_pricing import estimate_cost, pricing_config
from infrastructure.model_gateway import estimate_input
from contextvars import ContextVar
from infrastructure.model_client import create_model_client

ROOT = Path(__file__).resolve().parents[1]
EVALUATION_BUDGET = ContextVar('evaluation_budget', default=None)


def token_reservation(kwargs):
    output = kwargs.get('max_completion_tokens', kwargs.get('max_tokens', 8192))
    if type(output) is not int or output < 1:
        raise ValueError('The model output limit must be a positive integer.')
    payload = {'messages': kwargs.get('messages', []), 'tools': kwargs.get('tools', [])}
    estimate = estimate_input(payload['messages']) + estimate_input(payload['tools'])
    return max(estimate, len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))) + output + 512


def reported_usage(records):
    """Provider-reported usage stays separate from conservative budget occupancy."""
    complete = bool(records) and all(row.get('usage_complete') for row in records)
    return {'physical_calls': len(records),
        'unknown_usage_calls': sum(not row.get('usage_complete') for row in records),
        'prompt_tokens': sum(row['prompt_tokens'] for row in records) if complete else None,
        'completion_tokens': sum(row['completion_tokens'] for row in records) if complete else None,
        'total_tokens': sum(row['total_tokens'] for row in records) if complete else None,
        'accounted_tokens': sum(row.get('accounted_tokens', 0) for row in records)}

class SmokeLimit(ModelBudgetExceeded):
    pass


class Budget:
    def __init__(self,output,max_calls=12,max_tokens=100000):
        if not 1<=max_calls<=12 or not 1<=max_tokens<=100000:
            raise ValueError('Limits exceed the configured 12-call/100000-token safety ceiling')
        self.output=Path(output)
        if self.output.exists():
            raise FileExistsError('Existing audit file cannot be overwritten or reset')
        self.max_calls,self.max_tokens=max_calls,max_tokens
        self.physical_calls=0
        self.tokens=0
        self.pending=None
        self.records=[]
        self.stop_reason=''
        self.report={'mode':'real-model-smoke','results':[],'started_at':datetime.now(timezone.utc).isoformat()}

    @classmethod
    def resume(cls,output):
        report=json.loads(Path(output).read_text(encoding='utf-8'))
        state=report['budget']
        records=report['physical_requests']
        if (report.get('mode')!='real-model-smoke' or state['pending_reservation'] or state['stop_reason']
                or state['physical_calls']!=len(records)
                or [r['physical_attempt'] for r in records]!=list(range(1,len(records)+1))
                or any(not r.get('usage_complete') for r in records)
                or sum(r['accounted_tokens'] for r in records)!=state['accounted_tokens']
                or not 1<=state['max_calls']<=12 or not 1<=state['max_tokens']<=100000):
            raise SmokeLimit('Audit is incomplete, unknown, inconsistent or already stopped; cannot resume')
        value=object.__new__(cls)
        value.output=Path(output)
        value.max_calls,value.max_tokens=state['max_calls'],state['max_tokens']
        value.physical_calls,value.tokens=state['physical_calls'],state['accounted_tokens']
        value.pending=None
        value.records=records
        value.stop_reason=''
        value.report=report
        return value

    def save(self):
        self.report['budget']={'max_calls':self.max_calls,'max_tokens':self.max_tokens,
            'physical_calls':self.physical_calls,'accounted_tokens':self.tokens,
            'pending_reservation':self.pending['reservation'] if self.pending else 0,
            'stop_reason':self.stop_reason}
        self.report['physical_requests']=self.records
        self.output.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.output.with_suffix(self.output.suffix+'.tmp')
        temporary.write_text(json.dumps(self.report,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(self.output)

    def reserve(self,kwargs):
        deadline=self.report.get('deadline_utc')
        if deadline and datetime.now(timezone.utc)>=datetime.fromisoformat(deadline):
            self.stop_reason='deadline';self.save();raise SmokeLimit(self.stop_reason)
        reservation = token_reservation(kwargs)
        if self.pending is not None:
            raise SmokeLimit('Concurrent requests are not allowed')
        if self.stop_reason or self.physical_calls>=self.max_calls or self.tokens+reservation>self.max_tokens:
            self.stop_reason=self.stop_reason or ('call_limit' if self.physical_calls>=self.max_calls else 'insufficient_token_reservation')
            self.save()
            raise SmokeLimit(self.stop_reason)
        context=MODEL_CALL.get()
        self.pending={'purpose':context.purpose if context else 'unspecified','model':kwargs.get('model'),
            'event_key':MODEL_EVENT.get(),'reservation':reservation,'request':kwargs,
            'status':'reserved','started_at':datetime.now(timezone.utc).isoformat()}
        route=MODEL_ROUTE.get() or {}
        self.pending.update({k:route[k] for k in ('connection_id','profile_id','config_version') if k in route})
        self.save()

    def dispatch(self):
        if self.pending is None or self.pending.get('physical_attempt'):
            raise SmokeLimit('Unexpected or repeated physical HTTP request blocked')
        if self.physical_calls>=self.max_calls:
            self.stop_reason='call_limit'
            raise SmokeLimit(self.stop_reason)
        self.physical_calls+=1
        self.pending['physical_attempt']=self.physical_calls
        self.pending['status']='dispatched'
        self.records.append(self.pending)
        self.save()

    def before_request(self,request):
        if request.method!='POST' or not request.url.path.endswith('/chat/completions'):
            raise SmokeLimit('Only the configured chat-completion endpoint is allowed')
        self.dispatch()

    def settle(self,response=None,error=None,pricing=None):
        item=self.pending
        if item is None:
            return
        usage=getattr(response,'usage',None)
        prompt=getattr(usage,'prompt_tokens',None)
        completion=getattr(usage,'completion_tokens',None)
        total=getattr(usage,'total_tokens',None)
        valid=type(prompt) is int and prompt>=0 and type(completion) is int and completion>=0
        if valid and total is None:
            total=prompt+completion
        valid=valid and type(total) is int and total>=prompt+completion
        if item.get('physical_attempt'):
            charged=total if valid else item['reservation']
            self.tokens+=charged
            details=getattr(usage,'prompt_tokens_details',None)
            cached=getattr(details,'cached_tokens',None)
            item.update({'status':'completed' if error is None else 'failed','usage_complete':valid,
                         'prompt_tokens':prompt,'completion_tokens':completion,'total_tokens':total if valid else None,
                         'accounted_tokens':charged,'cached_tokens':cached,
                         'estimated_cost':estimate_cost(prompt,cached,completion,pricing or {}) if valid else None,
                         'cost_currency':(pricing or {}).get('currency',''),'pricing':pricing or {},
                         'error_type':type(error).__name__ if error else '',
                         'finished_at':datetime.now(timezone.utc).isoformat()})
            if response is not None and response.choices:
                item['finish_reason']=response.choices[0].finish_reason
                item['content']=response.choices[0].message.content
            if not valid:
                self.stop_reason='unknown_usage_stop'
            elif self.tokens>=self.max_tokens:
                self.stop_reason='token_limit'
            elif self.physical_calls>=self.max_calls:
                self.stop_reason='call_limit'
        self.pending=None
        self.save()


class CountingTransport(httpx.BaseTransport):
    def __init__(self,budget,inner=None):
        self.budget=budget
        self.inner=inner or httpx.HTTPTransport(retries=0)

    def handle_request(self,request):
        if request.method!='POST' or not request.url.path.endswith('/chat/completions'):
            raise SmokeLimit('Only the configured chat-completion endpoint is allowed')
        self.budget.dispatch()
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


class BoundedClient:
    def __init__(self,sdk,budget,output_cap=None):
        self.sdk,self.budget=sdk,budget
        self.calls=[]
        self.output_cap=output_cap
        self.network_metadata=getattr(sdk,'network_metadata',{'network_policy':'injected_or_legacy'})
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def close(self):
        self.sdk.close()

    def create(self,**kwargs):
        if 'max_completion_tokens' not in kwargs and 'max_tokens' not in kwargs:
            kwargs['max_completion_tokens'] = 8192
        if self.output_cap is not None:
            key='max_tokens' if 'max_tokens' in kwargs else 'max_completion_tokens'
            kwargs[key]=min(kwargs[key],self.output_cap)
        ceiling=self.budget.report.get('max_output_tokens')
        if ceiling:
            remaining=ceiling-sum(r.get('completion_tokens',0) or 0 for r in self.budget.records)
            if remaining<=0:
                self.budget.stop_reason='output_limit';self.budget.save();raise SmokeLimit('output_limit')
            key='max_tokens' if 'max_tokens' in kwargs else 'max_completion_tokens';kwargs[key]=min(kwargs[key],remaining)
        prices=MODEL_ROUTE.get() or pricing_config(kwargs.get('model'),os.getenv('LLM_MODEL_ID',''))
        self.budget.reserve(kwargs)
        self.calls.append(json.loads(json.dumps(kwargs)))
        started=time.monotonic()
        response=None
        error=None
        try:
            response=self.sdk.chat.completions.create(**kwargs)
            return response
        except BaseException as failure:
            error=failure
            raise
        finally:
            if self.budget.pending:
                self.budget.pending['elapsed_seconds']=round(time.monotonic()-started,3)
            self.budget.settle(response,error,prices)



class RecordingTransport:
    def __init__(self, receipt_prefix='eval-out'):
        self.messages = []
        self.receipt_prefix = receipt_prefix
    async def send(self, message):
        self.messages.append(message)
        return f'{self.receipt_prefix}-{len(self.messages)}' if self.receipt_prefix else None



def real_execution(name, token_budget, forwarded, report):
    """Bind a current budget; historical prose in recipes never grants authorization."""
    from contextlib import contextmanager, nullcontext
    if name == 'rail_mcp':
        forwarded.append('--authorize-live')
        return nullcontext()  # This recipe owns the independent MCP submission/deadline budget; no models.
    if name in {'rail_query','intercity_planning','model_routing'}:
        forwarded.append('--authorize-live')
    if name in {'evaluate_tasks', 'evaluate_v3_experiments', 'tool_catalog'} and '--live' not in forwarded:
        forwarded.append('--live')
    if name in {'smoke_summary_live', 'smoke_summary_web'}:
        for flag, ceiling in [('--max-tokens', token_budget), ('--max-calls', 12)]:
            if flag in forwarded:
                if int(forwarded[forwarded.index(flag)+1]) > ceiling:
                    raise ValueError('Scenario limit exceeds the explicit execution budget.')
            else:
                forwarded.extend([flag, str(ceiling)])
        # These recipes already use the shared persisted Budget and counted HTTP transport.
        return nullcontext()

    @contextmanager
    def guarded():
        from unittest.mock import patch
        output = Path(report).with_suffix('.requests.json') if report else (
            ROOT/'.upgrade-testing/evaluation'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')/'requests.json')
        if name=='rail_query' and token_budget>40000:
            raise ValueError('Rail query acceptance is limited to 40000 model tokens.')
        budget = Budget(output, max_calls=12 if name=='conversation_workflow' else 6 if name in {'intercity_planning','model_routing'} else 4 if name=='rail_query' else 12, max_tokens=token_budget)
        if name=='conversation_workflow':budget.report.update(max_output_tokens=16000,deadline_utc=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat())
        if name in {'rail_query','conversation_workflow'} and '--carry-model-receipt' in forwarded:
            import hashlib
            prior=Path(forwarded[forwarded.index('--carry-model-receipt')+1])
            previous=json.loads(prior.read_text(encoding='utf-8'))
            state,records=previous['budget'],previous['physical_requests']
            if (state['pending_reservation'] or state['max_calls']!=(4 if name=='rail_query' else 12) or state['max_tokens']>token_budget
                    or state['physical_calls']!=len(records) or any(not row.get('usage_complete') for row in records)
                    or sum(row['accounted_tokens'] for row in records)!=state['accounted_tokens']
                    or [row['physical_attempt'] for row in records]!=list(range(1,len(records)+1))):
                raise SmokeLimit('Prior receipt is not complete and consistent; cannot carry usage.')
            if name=='conversation_workflow':
                if previous.get('max_output_tokens')!=16000 or not previous.get('deadline_utc'):raise SmokeLimit('Prior output/time limits are missing')
                budget.report['deadline_utc']=previous['deadline_utc']
            budget.physical_calls,budget.tokens=state['physical_calls'],state['accounted_tokens']
            budget.records=records
            budget.report['continued_from']={'path':str(prior),'sha256':hashlib.sha256(prior.read_bytes()).hexdigest(),
                                            'previous_max_tokens':state['max_tokens']}
        from app import runtime_factory
        clients = []
        token = EVALUATION_BUDGET.set(budget)

        def client(*args, **kwargs):
            if kwargs.get('http_client') is not None:
                raise SmokeLimit('This recipe needs an explicit budget adapter for its custom HTTP client.')
            kwargs.pop('http_client',None)
            kwargs['max_retries'] = 0
            bounded = BoundedClient(create_model_client(*args,before_request=budget.before_request,**kwargs), budget,
                output_cap=2048 if name=='conversation_workflow' else 512 if name=='model_routing' else 1365 if name=='intercity_planning' else 1000 if name=='rail_query' else None)
            clients.append(bounded)
            return bounded

        try:
            with patch('openai.OpenAI', side_effect=client),patch.object(runtime_factory,'OpenAI',side_effect=client):
                yield budget
        finally:
            EVALUATION_BUDGET.reset(token)
            for sdk in clients:
                sdk.close()
            budget.save()
    return guarded()


def offline_execution():
    """Guard recipes as well as their default flags; in-process test transports remain usable."""
    from contextlib import contextmanager, ExitStack
    from unittest.mock import patch

    @contextmanager
    def guarded():
        with ExitStack() as stack:
            stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
            stack.enter_context(patch('requests.Session.request', side_effect=AssertionError('Offline evaluation forbids provider requests')))
            stack.enter_context(patch('httpx.HTTPTransport.handle_request', side_effect=AssertionError('Offline evaluation forbids model/provider HTTP')))
            stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('Offline evaluation forbids network connections')))
            yield
    return guarded()
