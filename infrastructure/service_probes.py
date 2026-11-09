"""Small, bounded service probes; never return provider errors or credentials."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import time
import uuid

from core.model_budget import MODEL_EVENT, MODEL_RUN, ModelBudgetExceeded, model_call_context
from core.model_config import CONFIG_KEY, ModelConfigError, parse_config, resolve_config
from core.chat_transport import ChatEvent
from infrastructure.amap_client import AmapClient
from infrastructure.model_gateway import ModelGateway
from infrastructure.public_http import PublicHTTPError
from infrastructure.model_client import create_model_client, model_failure_metadata
from infrastructure.search_client import SearchClient
from infrastructure.wendao_client import WendaoClient, WendaoError


# A fixed 16x16 PNG, unrelated to user uploads or chat history.
PROBE_IMAGE = ('data:image/png;base64,'
               'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAI0lEQVR4nGP8//8/AymAiSTVDKMaiANMRKqDg1ENxACSQwkAVW0DHeN02ZEAAAAASUVORK5CYII=')
CAPABILITY_LABELS={'text':'文本','json':'JSON 合同','tools':'工具调用','vision':'图片输入','embedding':'向量'}
SERVICE_LABELS={'search':'Tavily 搜索','amap':'高德地图 API','rail':'铁路 12306','wendao':'携程问道'}


async def probe_rail_config(raw):
    from infrastructure.mcp_client import RailMCPConfig, RemoteMCPClient
    from infrastructure.rail_client import validate_rail_tools
    config = RailMCPConfig.parse(raw)
    client = RemoteMCPClient(config, validate_tools=validate_rail_tools, max_calls=0, max_connections=1)
    try:
        client.start()
        status = await client.wait_ready()
        return {'status': 'ok', 'detail': '初始化与铁路工具合同通过；未查询车票，业务效果需另行限额验证。',
                'rail_mcp': status}
    finally:
        await client.close()


def _failure(error):
    if isinstance(error, ModelBudgetExceeded):
        return '模型预算或并发限制阻止了测试，请检查用量设置后重试。'
    code = model_failure_metadata(error).get('error_code') or getattr(error, 'code', '')
    return {
        'unauthorized': '认证失败，请检查已保存的 Key。',
        'authentication': '认证失败，请检查已保存的 Key。',
        'model_configuration': '所选模型档案不满足本次能力或连接要求，未改用其他供应商。',
        'forbidden': '服务拒绝访问，请检查 Key 权限或账户状态。',
        'rate_limited': '请求被限流，请稍后重试并检查服务额度。',
        'invalid_public_url': '模型地址无效或无法解析为公网 HTTPS 地址。',
        'invalid_response': '接口未返回有效结果，请检查模型名称或服务兼容性。',
        '10001': '高德 Key 无效，请确认使用 Web 服务类型的 Key。',
        '10003': '高德调用额度已用尽。',
        '10021': '高德接口请求被限流，请稍后重试。',
        'network_error': '网络连接或请求超时，请检查服务地址和网络。',
        'network': '网络连接或请求超时，请检查服务地址和网络。',
        'invalid_token': 'Token 格式无效，请检查已保存的凭据。',
        'business_error': '接口返回业务错误，请检查服务权限或账户额度。',
        'read_timeout': '请求超时，结果未知；未自动重试。',
        'result_unknown': '尚未取得明确结果，未自动重试。',
    }.get(str(code), '测试未通过，请检查服务配置、模型权限、网络和账户额度。')


def _model_probe(gateway, profile, capability, *, purpose='service_probe'):
    content=[{'type':'text','text':'Connectivity test. Reply only OK.'}]
    kwargs={}
    if capability=='json':
        content[0]['text']='Return a JSON object with exactly {"ok":true}.'
        kwargs['response_format']={'type':'json_object'}
    elif capability=='tools':
        content[0]['text']='Call probe_echo with ok=true. This tool has no side effects.'
        kwargs.update(tools=[{'type':'function','function':{'name':'probe_echo','parameters':{
            'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}}}],
            tool_choice={'type':'function','function':{'name':'probe_echo'}})
    elif capability=='vision':
        content[0]['text']='Identify the dominant color in the attached image. Reply with one color word.'
        content.append({'type':'image_url','image_url':{'url':PROBE_IMAGE,'detail':'low'}})
    with model_call_context(purpose,profile_id=profile.id if purpose=='service_probe' else ''):
        if capability=='embedding':
            result=gateway.create_embedding(model=profile.model_id,input=['fixed connectivity probe'])
            if not result.data or not result.data[0].embedding:raise PublicHTTPError('invalid_response')
        else:
            result=gateway.create(model=profile.model_id,messages=[{'role':'user','content':content}],max_completion_tokens=512,timeout=20,**kwargs)
            try:
                message=result.choices[0].message
                if capability=='tools':
                    tool=message.tool_calls[0].function
                    valid=tool.name=='probe_echo' and json.loads(tool.arguments).get('ok') is True
                elif capability=='json':valid=json.loads(message.content).get('ok') is True
                elif capability=='vision':valid=(message.content or '').strip().lower().rstrip('.') in {'white','白色','白'}
                else:valid=bool(message.content and message.content.strip())
                if not valid:raise ValueError()
            except (ValueError,KeyError,TypeError,AttributeError,IndexError):raise PublicHTTPError('invalid_response') from None
    return CAPABILITY_LABELS[capability]+'请求及返回合同通过；不代表其他能力或真实任务质量。'


def run_service_probes(values, store, *, configuration=None, profile_id='', capability=''):
    model_error=None
    try:config=configuration or resolve_config(values.get(CONFIG_KEY) or None,values)
    except ModelConfigError as error:
        if profile_id:raise
        config=parse_config('');model_error={'label':'模型配置','service':'model','model_id':'','status':'failed',
            'detail':_failure(error),'elapsed_ms':0}
    if profile_id:
        profile=config.profile(profile_id)
        if not profile or capability not in CAPABILITY_LABELS:raise ModelConfigError('请选择存在的模型档案和测试能力。')
        targets=[(profile,capability)]
    else:
        needs={}
        for role,identity in config.roles.items():
            needs.setdefault(identity,set()).update({'text','json','tools'} if role=='regular' else {'vision'} if role=='vision' else {'json'})
        for purpose,identity in config.purpose_overrides.items():
            needs.setdefault(identity,set()).add('vision' if purpose=='image_extract' else 'tools' if purpose=='tool_reasoning' else 'json')
        targets=[(profile,cap) for profile in config.profiles for cap in sorted(needs.get(profile.id,()))]
    # Same endpoint and model still need separate capability results; another connection is never deduplicated.
    unique={}
    for profile,cap in targets:unique.setdefault((profile.connection_id,profile.model_id,cap),(profile,cap))
    jobs=[('model',profile,cap) for profile,cap in unique.values()]
    if not profile_id:jobs.extend((service,None,'') for service in SERVICE_LABELS)
    gateway=ModelGateway(store,configuration=config,client_factory=create_model_client)
    event='service-probe:'+uuid.uuid4().hex
    def probe(index_job):
        index,(service,profile,cap)=index_job
        connection=config.connection(profile.connection_id) if profile else None
        result={'label':profile.name+' · '+CAPABILITY_LABELS[cap] if profile else SERVICE_LABELS[service],
            'model_id':profile.model_id if profile else '','connection_id':connection.id if connection else '',
            'profile_id':profile.id if profile else '','capability':cap,'service':service,'status':'skipped','detail':'','elapsed_ms':0}
        started=time.monotonic();token=MODEL_EVENT.set(event+(':'+profile.id+':'+cap if profile else ':'+service))
        run_token=MODEL_RUN.set(event)
        try:
            if service=='rail':
                from infrastructure.mcp_client import RailMCPConfig
                try:rail_config=RailMCPConfig.parse(values.get('RAIL_MCP_CONFIG',''))
                except ValueError:
                    result.update(status='failed',detail='铁路配置格式无效，请检查已保存的 Remote 配置。');return result
                configured=rail_config.enabled
                if not configured:result['detail']='未启用铁路服务，已跳过。';return result
            elif service=='wendao':
                if values.get('WENDAO_ENABLED','false').lower()!='true':
                    result['detail']='未启用携程问道，已跳过。';return result
                configured=bool(values.get('WENDAO_API_KEY'))
            else:
                configured=bool(connection and connection.api_key) if profile else bool(values.get('SEARCH_API_KEY' if service=='search' else 'AMAP_API_KEY'))
            if not configured or service=='model' and index>=12:
                result['detail']='未配置完整或超出本轮最多12次模型测试上限，已跳过。';return result
            if profile:detail=_model_probe(gateway,profile,cap)
            elif service=='rail':
                import asyncio
                detail=asyncio.run(probe_rail_config(values['RAIL_MCP_CONFIG']))['detail']
            elif service=='wendao':
                probe_event=ChatEvent('service_probe','group',event,'service-probes','local-owner','固定问道连通性测试')
                answer=WendaoClient(values['WENDAO_API_KEY'],True).query(probe_event,store.research,
                    '请用一句话简要介绍南京，不需要行程、票务或酒店信息。',timeout=20)
                if answer['status']!='completed':raise WendaoError(answer.get('error_code') or answer['status'])
                detail='问道查询接口已返回有效内容；本次固定短问句提交1次，供应商费用与用量未知。'
            elif service=='search':
                rows=SearchClient(values['SEARCH_API_KEY'],values['SEARCH_BASE_URL']).search('北京 官方 旅游')
                detail='搜索接口响应正常。' if rows else '搜索接口响应正常，本次未返回搜索结果。'
            else:
                data=AmapClient(values['AMAP_API_KEY'],max_attempts=1)._get('/v3/weather/weatherInfo',{'city':'110100','extensions':'base'})
                if not isinstance(data.get('lives'),list) or not data['lives']:raise PublicHTTPError('invalid_response')
                detail='高德天气接口响应正常。'
            result.update(status='ok',detail=detail)
        except Exception as error:result.update(status='failed',detail=_failure(error))
        finally:MODEL_EVENT.reset(token);MODEL_RUN.reset(run_token)
        result['elapsed_ms']=round((time.monotonic()-started)*1000);return result
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(probe,enumerate(jobs)))
    finally:gateway.close()
    if model_error:results.insert(0,model_error)
    return {'results':results,'tested_at':datetime.now(timezone.utc).isoformat(),'scope':'saved',
        'summary':{status:sum(row['status']==status for row in results) for status in ('ok','failed','skipped')}}
