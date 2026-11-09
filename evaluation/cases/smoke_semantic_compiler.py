"""Production-path intent-only debug; explicit real mode/budget, no business writes."""
import argparse,json,tempfile
from pathlib import Path
from openai import OpenAI
from agents.intent_compiler import IntentCompiler,IntentCompilationError
from core.chat_transport import ChatEvent
from core.intent_ir import to_jsonable
from core.web_settings import WebSettings
from core.model_budget import MODEL_EVENT
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from evaluation.support import ROOT,EVALUATION_BUDGET


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--message',action='append')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    texts=args.message or ['我想从武汉去南京玩两天，计划30号下午出发，11月1号下午回来，帮我规划一下旅程。']
    if not 1<=len(texts)<=3:raise ValueError('Each debug run accepts one to three requests.')
    budget=EVALUATION_BUDGET.get()
    if budget is None:raise ValueError('Use the evaluation entry with an explicit real-model mode and token budget.')
    budget.max_calls=min(budget.max_calls,3)
    settings=WebSettings.from_env().travel_settings()
    configuration=settings.model_configuration
    _,regular=configuration.select('unspecified',force_default=True)
    results=[]
    with tempfile.TemporaryDirectory(prefix='intent-debug-') as directory:
        store=MemoryStore(Path(directory)/'debug.db')
        gateway=ModelGateway(store,configuration=configuration,client_factory=OpenAI,default_model=regular.model_id)
        compiler=IntentCompiler(gateway,regular.model_id,store.diagnostics)
        try:
            for index,text in enumerate(texts):
                event=ChatEvent('evaluation','group',str(index),'isolated-debug','tester',text)
                store.conversations.save_chat_message(event.event_key,'evaluation','isolated-debug','tester',str(index),'','user',text)
                token=MODEL_EVENT.set(event.event_key)
                row={'index':index+1,'scope':'intent_parse_only','passed':False}
                try:
                    ir=compiler.try_compile(text)
                    row.update(passed=ir is not None,ir=to_jsonable(ir) if ir else None)
                except IntentCompilationError as error:row['error_code']=error.code
                finally:MODEL_EVENT.reset(token)
                with store._connect() as db:
                    row['diagnostics']=[{'stage':r['stage'],'details':json.loads(r['details_json'])} for r in db.execute(
                        'SELECT stage,details_json FROM execution_diagnostics WHERE event_key=? ORDER BY id',(event.event_key,))]
                results.append(row)
                if not row['passed']:break
        finally:gateway.close()
    report={'results':results,'summary':{'total':len(results),'passed':sum(r['passed'] for r in results)},
        'business_actions':0,'physical_model_calls':budget.physical_calls,
        'note':'Full current intent prompt; production SDK/proxy/timeout policy; no requirement extraction or planning.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report['summary']))
    raise SystemExit(0 if all(r['passed'] for r in results) else 1)


if __name__=='__main__':main()
