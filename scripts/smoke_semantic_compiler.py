"""Live semantic compiler smoke check; parses only and never performs a business write."""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from openai import OpenAI

from agents.intent_compiler import IntentCompiler
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway


def main():
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / '.env')
    with tempfile.TemporaryDirectory(prefix='semantic-compiler-') as folder:
        store = MemoryStore(Path(folder) / 'smoke.db')
        client = ModelGateway(store, OpenAI(api_key=os.getenv('LLM_API_KEY', ''),
            base_url=os.getenv('LLM_BASE_URL', ''), max_retries=1))
        compiler = IntentCompiler(client, os.getenv('LLM_MODEL_ID', ''))
        texts = [
            '10月2日不去湖北省博物馆了',
            '确认取消调整，请变更行程',
            '五分钟后提醒我登录王者做任务',
        ]
        results = []
        for text in texts:
            ir = compiler.compile(text)
            results.append({'text': text, 'action': ir.action,
                'operations': [(item.domain, item.operation) for item in ir.operations],
                'requires_confirmation': ir.requires_confirmation})
        client.close()
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
