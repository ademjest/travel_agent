"""Small, bounded service probes; never return provider errors or credentials."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace

from core.model_budget import ModelBudgetExceeded, model_call_context
from infrastructure.amap_client import AmapClient
from infrastructure.model_gateway import ModelGateway
from infrastructure.public_http import PublicHTTPError, request_public
from infrastructure.search_client import SearchClient


# A fixed 16x16 PNG, unrelated to user uploads or chat history.
PROBE_IMAGE = ('data:image/png;base64,'
               'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAI0lEQVR4nGP8//8/AymAiSTVDKMaiANMRKqDg1ENxACSQwkAVW0DHeN02ZEAAAAASUVORK5CYII=')
MODEL_FIELDS = (('LLM_MODEL_ID', '默认模型'), ('LLM_INTENT_MODEL_ID', '意图解析'),
                ('LLM_SUMMARY_MODEL_ID', '会话摘要'), ('LLM_RESEARCH_MODEL_ID', '攻略研究'))


def _failure(error):
    if isinstance(error, ModelBudgetExceeded):
        return '模型预算或并发限制阻止了测试，请检查用量设置后重试。'
    code = getattr(error, 'code', '')
    return {
        'unauthorized': '认证失败，请检查已保存的 Key。',
        'forbidden': '服务拒绝访问，请检查 Key 权限或账户状态。',
        'rate_limited': '请求被限流，请稍后重试并检查服务额度。',
        'invalid_public_url': '模型地址无效或无法解析为公网 HTTPS 地址。',
        'invalid_response': '接口未返回有效结果，请检查模型名称或服务兼容性。',
        '10001': '高德 Key 无效，请确认使用 Web 服务类型的 Key。',
        '10003': '高德调用额度已用尽。',
        '10021': '高德接口请求被限流，请稍后重试。',
        'network_error': '网络连接或请求超时，请检查服务地址和网络。',
        'network': '网络连接或请求超时，请检查服务地址和网络。',
    }.get(str(code), '测试未通过，请检查服务配置、模型权限、网络和账户额度。')


def _probe_gateway(values, store):
    def create(**kwargs):
        kwargs.pop('timeout', None)
        raw, _, _ = request_public(values['LLM_BASE_URL'].rstrip('/') + '/chat/completions',
            payload=kwargs, headers={'Authorization': 'Bearer ' + values['LLM_API_KEY']},
            timeout=20, max_bytes=100_000)
        try:
            value = json.loads(raw)
            choices = value['choices']
            if not isinstance(choices, list) or not choices or not choices[0]['message'].get('content'):
                raise ValueError()
            usage = value.get('usage')
            return SimpleNamespace(choices=choices, usage=SimpleNamespace(**usage) if isinstance(usage, dict) else None)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PublicHTTPError('invalid_response') from None

    return ModelGateway(store, SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
                        default_model=values['LLM_MODEL_ID'])


def _model_probe(gateway, model, vision):
    content = [{'type': 'text', 'text': 'This is a connectivity test. Reply only OK.'}]
    if vision:
        content.append({'type': 'image_url', 'image_url': {'url': PROBE_IMAGE, 'detail': 'low'}})
    with model_call_context('service_probe', force_default_model=True):
        gateway.create(model=model, messages=[{'role': 'user', 'content': content}], max_completion_tokens=512)
    return '图片输入请求通过。' if vision else '文本推理请求通过。'


def run_service_probes(values, store):
    jobs = []
    default = values['LLM_MODEL_ID'].strip()
    models = {}
    for field, label in MODEL_FIELDS:
        model = values[field].strip() or default
        models.setdefault(model, []).append(label)
    for model, roles in models.items():
        jobs.append((' / '.join(roles), model, 'model', False))
    vision = values['LLM_VISION_MODEL_ID'].strip() or default
    jobs.extend([('视觉模型', vision, 'model', True), ('Tavily 搜索', '', 'search', False),
                 ('高德地图 API', '', 'amap', False)])
    gateway = (_probe_gateway(values, store) if any(job[2] == 'model' and job[1] for job in jobs)
               and values['LLM_API_KEY'] and values['LLM_BASE_URL'] else None)

    def probe(job):
        label, model, service, vision = job
        result = {'label': label, 'model_id': model, 'service': service, 'status': 'skipped',
                  'detail': '', 'elapsed_ms': 0}
        configured = ((model and values['LLM_API_KEY'] and values['LLM_BASE_URL']) if service == 'model'
                      else values['SEARCH_API_KEY'] if service == 'search' else values['AMAP_API_KEY'])
        if not configured:
            result['detail'] = '未配置完整，已跳过。'
            return result
        started = time.monotonic()
        try:
            if service == 'model':
                detail = _model_probe(gateway, model, vision)
            elif service == 'search':
                rows = SearchClient(values['SEARCH_API_KEY'], values['SEARCH_BASE_URL']).search('北京 官方 旅游')
                detail = '搜索接口响应正常。' if rows else '搜索接口响应正常，本次未返回搜索结果。'
            else:
                data = AmapClient(values['AMAP_API_KEY'], max_attempts=1)._get('/v3/weather/weatherInfo',
                    {'city': '110100', 'extensions': 'base'})
                if not isinstance(data.get('lives'), list) or not data['lives']:
                    raise PublicHTTPError('invalid_response')
                detail = '高德天气接口响应正常。'
            result.update(status='ok', detail=detail)
        except Exception as error:
            result.update(status='failed', detail=_failure(error))
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(probe, jobs))
    return {'results': results, 'tested_at': datetime.now(timezone.utc).isoformat(),
            'scope': 'saved', 'summary': {status: sum(row['status'] == status for row in results)
                                         for status in ('ok', 'failed', 'skipped')}}
