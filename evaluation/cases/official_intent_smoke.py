"""At most three real compiler calls; no provider calls or production database."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    from openai import OpenAI
    from agents.intent_compiler import IntentCompiler
    from core.intent_ir import to_jsonable
    from evaluation.support import EVALUATION_BUDGET
    budget=EVALUATION_BUDGET.get()
    if budget is None:
        raise ValueError('Run through the explicit real-model evaluation mode')
    budget.max_calls=3
    sdk=OpenAI(api_key=os.getenv('LLM_API_KEY',''),base_url=os.getenv('LLM_BASE_URL'),max_retries=0)
    def create(**kwargs):
        return sdk.chat.completions.create(**kwargs,max_tokens=1024)
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    compiler=IntentCompiler(client,os.getenv('LLM_INTENT_MODEL_ID') or os.getenv('LLM_MODEL_ID',''))
    results=[]
    try:
        for text in ('我想去参观湖北省博物馆，能给我找到他的官方网站链接吗？',
                     '湖北省博物馆官网是什么？','请搜索南京博物院的官方网站链接'):
            ir=compiler.compile(text)
            passed=(ir.action=='read' and len(ir.operations)==1 and ir.operations[0].domain=='search'
                    and ir.operations[0].operation=='query_official_website')
            results.append({'request':text,'passed':passed,'ir':to_jsonable(ir)})
    finally:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps({'results':results,'summary':{'total':len(results),'passed':sum(r['passed'] for r in results)},
            'provider_calls':0,'physical_model_calls':budget.physical_calls},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'total':len(results),'passed':sum(r['passed'] for r in results)}))


if __name__=='__main__':
    main()
