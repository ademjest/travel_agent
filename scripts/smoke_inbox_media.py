"""Queued real-model weather/image flow. Uses synthetic pixels, temporary DB and no QQ sends."""
import asyncio
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from app.runtime_factory import build_runtime
from core.settings import Settings, OneBotSettings
from infrastructure.memory_store import MemoryStore
from services.inbox_worker import InboxWorker


class RecordingTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        return f'local-{len(self.messages)}'


def synthetic_ticket():
    image = Image.new('RGB', (1000, 500), 'white')
    draw = ImageDraw.Draw(image)
    font_path = Path('C:/Windows/Fonts/msyh.ttc')
    if not font_path.exists():
        raise RuntimeError('该本地测试需要微软雅黑字体；可改用本机可用的中文字体。')
    font = ImageFont.truetype(str(font_path), 42)
    for y, line in zip((60, 155, 250, 345), ('旅行测试票', '目的地：武汉', '使用日期：2030年10月1日', '仅用于自动化测试')):
        draw.text((60, y), line, fill='black', font=font)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


async def main():
    load_dotenv()
    settings = Settings('', '', frozenset({'queue-smoke'}), os.getenv('AMAP_API_KEY', ''),
        os.getenv('LLM_API_KEY', ''), os.getenv('LLM_BASE_URL', ''), os.getenv('LLM_MODEL_ID', ''))
    transport = RecordingTransport()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'smoke.db'
        store = MemoryStore(path)
        def build(store):
            runtime = build_runtime(settings, platform='onebot', store=store, transport=transport,
                reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group == 'queue-smoke')
            adapter = OneBotAdapter(OneBotSettings('http://localhost', 'unused', 'unused', frozenset({'queue-smoke'})), runtime.application, store)
            return runtime, InboxWorker(store, adapter)
        runtime, worker = build(store)
        def payload(text, key, *, image=False, at=True):
            segments = ([{'type': 'at', 'data': {'qq': 'queue-bot'}}] if at else [])
            segments.append({'type': 'text', 'data': {'text': text}})
            if image:
                segments.append({'type': 'image', 'data': {'name': 'test-ticket.png', 'url': 'https://example.test/synthetic', 'content_type': 'image/png'}})
            return {'post_type': 'message', 'message_type': 'group', 'group_id': 'queue-smoke', 'user_id': 'queue-user',
                    'self_id': 'queue-bot', 'message_id': key, 'message': segments}
        accepted = await worker.submit(payload('现在天气怎么样', 'weather'))
        await worker.submit(payload('武汉', 'city', at=False))
        assert accepted['status'] == 'accepted' and not transport.messages
        await worker.run_once()
        await worker.run_once()
        assert store.inbox.get('onebot:group:queue-smoke:city')['status'] == 'completed'
        assert '武汉' in transport.messages[-1].payload['message']
        image_bytes = synthetic_ticket()
        worker.cache.downloader = lambda attachment: (image_bytes, 'image/png')
        await worker.submit(payload('这张票哪天使用？', 'image', image=True))
        await worker.capture_once()
        await worker.run_once()
        answer = transport.messages[-1].payload['message']
        assert re.search(r'2030\D+10\D+0?1', answer), answer
        with store._connect() as connection:
            before = connection.execute('SELECT count(*) FROM model_calls').fetchone()[0]
        await worker.submit(payload('这张票哪天使用？', 'image', image=True))
        assert not await worker.run_once()
        with store._connect() as connection:
            assert connection.execute('SELECT count(*) FROM model_calls').fetchone()[0] == before
        restarted_store = MemoryStore(path)
        restarted, next_worker = build(restarted_store)
        await next_worker.submit(payload('刚才那张票的使用日期是什么？', 'followup'))
        await next_worker.run_once()
        assert re.search(r'2030\D+10\D+0?1', transport.messages[-1].payload['message']), transport.messages[-1].payload['message']
        with restarted_store._connect() as connection:
            usage = dict(connection.execute('''SELECT count(*) AS calls, sum(prompt_tokens) AS prompt_tokens,
                sum(completion_tokens) AS completion_tokens, count(job_id) AS job_linked_calls FROM model_calls''').fetchone())
            assert usage['calls'] == usage['job_linked_calls']
        print(json.dumps({'model': settings.llm_model_id, 'status': 'passed', 'checks': [
            'queued_weather_continuation', 'durable_image_capture', 'live_synthetic_ticket_reading',
            'duplicate_input_no_extra_model_call', 'image_context_after_restart', 'job_linked_model_usage'], 'usage': usage}, ensure_ascii=False, indent=2))
        runtime.travel_agent.client.close()
        restarted.travel_agent.client.close()


if __name__ == '__main__':
    asyncio.run(main())
