"""U6 fixed-input model routing acceptance. No production chat, map, rail or search requests."""
import argparse
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from evaluation.support import ROOT,EVALUATION_BUDGET


def live(args):
    from app import runtime_factory
    from core.web_settings import WebSettings
    from core.model_config import parse_config
    from core.model_budget import MODEL_EVENT,MODEL_ROUTE
    from infrastructure.memory_store import MemoryStore
    from infrastructure.model_gateway import ModelGateway,usage_report
    from infrastructure.service_probes import _model_probe,_failure
    budget=EVALUATION_BUDGET.get()
    if budget is None or budget.max_calls>6 or budget.max_tokens>30000:
        raise ValueError('Use the evaluation runner with an explicit model budget <=30000.')
    settings=WebSettings.from_env().travel_settings()
    original=settings.model_configuration
    if not original.configured:raise ValueError('The existing model connection is not configured.')
    # Exercise the new block contract in memory, retaining every existing target and credential.
    config=parse_config(original.as_dict(include_keys=True))
    captures=[];rows=[]
    def factory(**kwargs):
        client=runtime_factory.OpenAI(**kwargs)
        def create(**request):
            result=client.chat.completions.create(**request)
            route=MODEL_ROUTE.get() or {}
            captures.append({'route':{k:route.get(k) for k in ('connection_id','profile_id','config_version')},
                'request':request,'response':result.model_dump(mode='json')})
            return result
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=client.close,
            network_metadata=getattr(client,'network_metadata',{}))
    with tempfile.TemporaryDirectory(prefix='u6-models-') as folder:
        store=MemoryStore(Path(folder)/'models.db')
        gateway=ModelGateway(store,configuration=config,client_factory=factory)
        try:
            for purpose,capability in (('tool_answer','text'),('intent_parse','json'),('trip_plan','json'),
                                       ('tool_reasoning','tools'),('image_extract','vision')):
                row={'purpose':purpose,'capability':capability,'passed':False}
                token=MODEL_EVENT.set('u6-fixed:'+purpose)
                try:
                    connection,profile=config.select(purpose)
                    row.update(connection_id=connection.id,profile_id=profile.id,model_id=profile.model_id)
                    row['detail']=_model_probe(gateway,profile,capability,purpose=purpose)
                    row['passed']=True
                except Exception as error:
                    row.update(error_type=type(error).__name__,detail=_failure(error))
                finally:MODEL_EVENT.reset(token)
                rows.append(row)
                if not row['passed']:break
            usage=usage_report(store)
            with store._connect() as db:
                ledger=[{k:r[k] for k in ('call_id','purpose','model_id','connection_id','profile_id','status','currency','estimated_cost','prompt_tokens','completion_tokens')}
                        for r in db.execute('SELECT * FROM model_usage_ledger ORDER BY started_at')]
        finally:gateway.close()
    report={'mode':'live_provider','passed':len(rows)==5 and all(r['passed'] for r in rows),'results':rows,
        'limits':{'model_calls':6,'tokens':30000,'output_per_call':512,'aggregate_output_tokens':3072},
        'physical_model_calls':budget.physical_calls,'usage':usage,'ledger':ledger,'captures':captures,
        'non_model_provider_calls':0,'production_writes':0,'configuration_saved':False,
        'note':'Fixed text/JSON/tool requests and a built-in white image only; not a complete trip or model quality evaluation.'}
    text=json.dumps(report,ensure_ascii=False,indent=2,default=str)
    for connection in config.connections:
        if connection.api_key:text=text.replace(connection.api_key,'[redacted]')
        text=text.replace(connection.base_url,'[configured model endpoint]')
    args.output.write_text(text+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'calls':budget.physical_calls,'results':rows},ensure_ascii=False))
    return report['passed']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--authorize-live',action='store_true')
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists():raise ValueError('Use a fresh evidence path.')
    if args.authorize_live:passed=live(args)
    else:
        import sys
        sys.path.insert(0,str(ROOT/'tests'))
        from test_model_config import ModelConfigTests,MultiProviderGatewayTests
        from test_service_settings import ModelSettingsTests
        suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in
            (ModelConfigTests,MultiProviderGatewayTests,ModelSettingsTests))
        result=unittest.TextTestRunner(verbosity=2).run(suite);passed=result.wasSuccessful()
        args.output.write_text(json.dumps({'mode':'offline','passed':passed,'tests':result.testsRun,'real_calls':0}),encoding='utf-8')
    raise SystemExit(0 if passed else 1)


if __name__=='__main__':main()
