import asyncio
from core.runtime_identity import runtime_status, retrieval_status
from core.commands import request_priority
from contextlib import asynccontextmanager, nullcontext
import io
import mimetypes
from pathlib import Path
import secrets
import uuid
import zipfile
from typing import Literal

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from adapters.web_adapter import WebAdapter
from adapters.web_transport import WebReplyRenderer, WebTransport
from app.runtime_factory import build_runtime
from core.data_paths import PROJECT_ROOT
from core.web_settings import WebSettings, web_data_lease
from core.web_lifecycle import ConversationDeleted, publish_files
from infrastructure.memory_store import MemoryStore
from infrastructure.web_repository import OWNER, WebConflict, WebReferenceError, WebRepository
from infrastructure.document_formats import validate_office_archive
from services.inbox_worker import InboxWorker
from services.conversation_deletion import ConversationDeletionService
from infrastructure.env_settings_store import EnvSettingsStore, SettingsConflict, SettingsInputError
from infrastructure.preference_repository import PreferenceConflict
from infrastructure.planning_repository import PlanningConflict
from infrastructure.model_gateway import usage_report
from infrastructure.trip_repository import DraftActionConflict
from core.chat_transport import ChatEvent


COOKIE = 'travel_web_session'
MAX_UPLOAD = 5 * 1024 * 1024
DOCUMENTS = {'.txt', '.md', '.docx', '.xlsx'}
IMAGES = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP'}


class MessageInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(default='', max_length=8000)
    upload_ids: list[str] = Field(default_factory=list, max_length=8)
    client_request_id: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    quoted_message_id: str | None = Field(default=None,max_length=256,min_length=1)


class TitleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=80)


class RailQueryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    period: Literal['current','all_day','morning','afternoon','evening']
    client_request_id: str = Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9_-]+$')


class ConfirmationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


class DraftActionInput(ConfirmationInput):
    action: Literal['revalidate','discard']
    version: int = Field(strict=True,ge=1)


class HotelActionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['select','accept_dates','reject_dates']
    task_id: str = Field(min_length=1,max_length=100)
    task_version: int = Field(strict=True,ge=1)
    query_index: int | None = Field(default=None,strict=True,ge=0,le=7)
    candidate_set_id: str | None = Field(default=None,max_length=100)
    candidate_set_version: int | None = Field(default=None,strict=True,ge=1)
    candidate_id: str | None = Field(default=None,max_length=100)
    proposal_id: str | None = Field(default=None,max_length=100)
    proposal_version: int | None = Field(default=None,strict=True,ge=1)
    client_request_id: str = Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9_-]+$')


class TripEditInput(ConfirmationInput):
    field: str = Field(max_length=30)
    value: str = Field(max_length=60)
    draft_id: str | None = Field(default=None,max_length=100)
    draft_version: int | None = Field(default=None,ge=1)


def create_web_app(settings=None, travel_settings=None, *, start_workers=True, settings_store=None):
    settings = settings or WebSettings.from_env()
    settings.validate()
    travel_settings = travel_settings or settings.travel_settings()
    if settings_store is None:
        try:
            settings_store = EnvSettingsStore(PROJECT_ROOT)
        except (ValueError, OSError):
            # A malformed service-settings file must not disable existing Web conversations.
            pass
    store = MemoryStore(settings.data_dir / 'travel_bot.db')
    repository = WebRepository(store)
    runtime = build_runtime(travel_settings, platform='web', store=store,
                            transport=WebTransport(repository), reply_renderer=WebReplyRenderer(),
                            group_allowed=repository.allows)
    from functools import partial
    from app.conversation_context import web_context
    build_context = partial(web_context, repository,clock=lambda:runtime.application.trip_service.clock())
    adapter = WebAdapter(runtime.application, repository, context_builder=build_context)
    worker = InboxWorker(store, adapter)
    deletion = ConversationDeletionService(repository)

    async def maintain():
        while True:
            await asyncio.to_thread(runtime.maintenance_service.run_once)
            await asyncio.to_thread(repository.clean_uploads)
            await asyncio.sleep(3600)

    @asynccontextmanager
    async def lifespan(app):
        with web_data_lease(repository.root) if start_workers else nullcontext():
            if start_workers:
                runtime.supervisor.start('web-inbox', worker.run)
                runtime.supervisor.start('web-outbox', runtime.outbox_worker.run)
                runtime.supervisor.start('web-reminders', runtime.reminder_scheduler.run)
                runtime.supervisor.start('web-maintenance', maintain)
                runtime.supervisor.start('web-deletions', deletion.run)
                if getattr(store, 'semantic_index', None) is not None:
                    runtime.supervisor.start('web-local-index', store.semantic_index.run)
            try:
                yield
            finally:
                await runtime.supervisor.stop()
                model = getattr(getattr(runtime.application, 'travel_agent', None), 'client', None)
                if getattr(model, 'close', None):
                    await asyncio.to_thread(model.close)

    app = FastAPI(title='彼岸 · 本地旅行助手', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime, app.state.repository, app.state.worker = runtime, repository, worker
    app.state.deletion = deletion
    allowed_hosts = {f'127.0.0.1:{settings.port}', f'localhost:{settings.port}'}
    if settings.dev:
        allowed_hosts.update({'127.0.0.1:5173', 'localhost:5173'})

    @app.middleware('http')
    async def local_access(request: Request, call_next):
        host = request.headers.get('host', '').lower()
        if host not in allowed_hosts:
            return JSONResponse({'detail': '不允许的本地访问地址。'}, status_code=403)
        origin = request.headers.get('origin')
        if request.headers.get('sec-fetch-site') == 'cross-site' or (origin is not None and origin != f'http://{host}'):
            return JSONResponse({'detail': '不允许跨站访问本地助手。'}, status_code=403)
        path = request.url.path
        if path.startswith('/api/') and path != '/api/bootstrap':
            csrf = await asyncio.to_thread(repository.session, request.cookies.get(COOKIE))
            if not csrf:
                return JSONResponse({'detail': '本地会话已过期，请刷新页面。'}, status_code=401)
            if request.method not in {'GET', 'HEAD'} and not secrets.compare_digest(request.headers.get('x-csrf-token', ''), csrf):
                return JSONResponse({'detail': '请求校验失败，请刷新页面。'}, status_code=403)
        if request.method in {'POST', 'PATCH', 'PUT'}:
            limit = MAX_UPLOAD + 64 * 1024 if path == '/api/uploads' else 128 * 1024
            chunks, length = [], 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > limit:
                    return JSONResponse({'detail': '上传或消息超过大小限制。'}, status_code=413)
                chunks.append(chunk)
            request._body = b''.join(chunks)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if path.startswith('/api/') or path == '/':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(LookupError)
    async def missing(request, error):
        return JSONResponse({'detail': str(error)}, status_code=404)

    @app.exception_handler(ConversationDeleted)
    async def gone(request, error):
        return JSONResponse({'detail': str(error), 'conversation_id': error.identity}, status_code=410)

    @app.exception_handler(WebConflict)
    async def conflict(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    conversations_api = APIRouter()
    settings_api = APIRouter()
    operations_api = APIRouter()

    @operations_api.get('/api/bootstrap')
    def bootstrap(request: Request):
        token = request.cookies.get(COOKIE)
        csrf = repository.session(token)
        if not csrf:
            token, csrf = repository.new_session()
        response = JSONResponse({'csrf_token': csrf, 'version': '0.1.0', 'mode': 'local',
                                 'llm_configured': travel_settings.llm_configured,
                                 'amap_configured': bool(travel_settings.amap_api_key)})
        response.set_cookie(COOKIE, token, httponly=True, samesite='strict', max_age=7*86400)
        return response

    @conversations_api.get('/api/conversations')
    def conversations():
        return repository.conversations()

    @operations_api.get('/api/runtime-status')
    def runtime_information():
        gateway=runtime.travel_agent.client if runtime.travel_agent else None
        return runtime_status(store, runtime.travel_agent.model if runtime.travel_agent else travel_settings.llm_model_id,
                              getattr(gateway,'configuration',None))

    @conversations_api.get('/api/conversations/{identity}/retrieval-status')
    def retrieval_information(identity: str):
        repository.conversation(identity)
        return retrieval_status(store, f'web:{identity}')

    def service_settings():
        nonlocal settings_store
        if settings_store is None:
            settings_store = EnvSettingsStore(PROJECT_ROOT)
        return settings_store

    @settings_api.get('/api/settings/services')
    def get_service_settings():
        try:
            status = service_settings().status()
            if runtime.rail_client is not None:
                status['rail_mcp']['runtime'] = runtime.rail_client.mcp.status()
            return status
        except Exception:
            raise HTTPException(400, '配置读取失败，请检查文件格式与权限。') from None

    @settings_api.patch('/api/settings/services')
    async def save_service_settings(request: Request):
        # Deliberately no Pydantic input echo in validation errors on credential routes.
        try:
            body = await request.json()
            if not isinstance(body, dict) or not {'version','changes'} <= set(body) or set(body)-{'version','changes','model_config'}:
                raise ValueError()
            extra={'model_config':body['model_config']} if 'model_config' in body else {}
            return await asyncio.to_thread(service_settings().save, body['version'], body['changes'],**extra)
        except SettingsConflict:
            raise HTTPException(409, '配置已变化或正在保存，请刷新后重试。') from None
        except SettingsInputError as error:
            raise HTTPException(400, str(error)) from None
        except Exception:
            raise HTTPException(400, '保存失败：检查字段、HTTPS 地址、端点更换时的新 Key、Git 忽略及文件权限。原有配置未被替换。') from None

    @settings_api.post('/api/settings/services/test')
    async def test_service_settings(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):raise ValueError()
            if body.get('service')=='model':
                if set(body)!={'service','profile_id','capability'} or not all(isinstance(body[k],str) for k in body):raise ValueError()
                return await asyncio.to_thread(service_settings().test_all,store,profile_id=body['profile_id'],capability=body['capability'])
            if set(body) != {'service'} or body['service'] not in ('search', 'llm', 'rail', 'all'):
                raise ValueError()
            if body['service'] == 'all':
                return await asyncio.to_thread(service_settings().test_all, store)
            return await asyncio.to_thread(service_settings().test, body['service'])
        except SettingsConflict:
            raise HTTPException(409, '服务测试正在运行，请等待完成后再试。') from None
        except Exception:
            raise HTTPException(400, '连接测试未通过：请检查已保存的配置、网络和服务额度。未回传供应商错误正文。') from None

    @settings_api.get('/api/settings/model-usage')
    def model_usage():
        return usage_report(store)

    def preference_event():
        return ChatEvent('web', 'group', 'preferences-settings', '', OWNER, '')

    @settings_api.get('/api/preferences')
    def get_preferences():
        return runtime.application.preference_service.snapshot(preference_event())

    @settings_api.patch('/api/preferences')
    async def edit_preferences(request: Request):
        try:
            body = await request.json()
            if (not isinstance(body, dict) or set(body) - {'version', 'values', 'enabled', 'suggestions', 'clear'}
                    or 'version' not in body or type(body.get('clear', False)) is not bool):
                raise ValueError('偏好请求字段无效。')
            return await asyncio.to_thread(runtime.application.preference_service.update, preference_event(), body['version'], body.get('values', {}),
                enabled=body.get('enabled'), suggestions=body.get('suggestions'), clear=body.get('clear', False))
        except PreferenceConflict:
            raise HTTPException(409, '偏好已经变化，请刷新后重试。') from None
        except (ValueError, TypeError):
            raise HTTPException(400, '偏好字段无效，请检查字段、单位和版本。') from None

    @conversations_api.get('/api/conversations/{identity}/research')
    def research_results(identity: str):
        repository.conversation(identity)
        event = ChatEvent('web', 'group', '', identity, OWNER, '')
        return runtime.application.research_service.web_reports(event)

    @conversations_api.post('/api/conversations', status_code=201)
    def create_conversation():
        return repository.create_conversation()

    @conversations_api.patch('/api/conversations/{identity}')
    def rename(identity: str, body: TitleInput):
        if not body.title.strip():
            raise HTTPException(422, '标题不能为空。')
        return repository.rename(identity, body.title.strip())

    @conversations_api.delete('/api/conversations/{identity}/plans/{artifact_id}')
    def delete_plan(identity: str, artifact_id: str, version: int):
        event=ChatEvent('web','group','',identity,OWNER,'')
        repository.conversation(identity)
        try:
            store.planning.delete(event,artifact_id,version)
        except PlanningConflict as error:
            raise HTTPException(409,str(error)) from None
        return {'status':'deleted'}

    @conversations_api.get('/api/conversations/{identity}/deletion-summary')
    def deletion_summary(identity: str):
        return deletion.summary(identity)

    @conversations_api.delete('/api/conversations/{identity}', status_code=202)
    async def delete_conversation(identity: str):
        result = await asyncio.to_thread(deletion.request, identity)
        deletion.wakeup.set()
        return result

    @conversations_api.get('/api/conversation-deletions')
    def deletion_jobs():
        return deletion.list_jobs()

    @conversations_api.get('/api/conversation-deletions/{identity}')
    def deletion_status(identity: str):
        return deletion.status(identity)

    @conversations_api.post('/api/conversation-deletions/{identity}/retry', status_code=202)
    async def retry_deletion(identity: str):
        result = await asyncio.to_thread(deletion.retry, identity)
        deletion.wakeup.set()
        return result

    @conversations_api.get('/api/conversations/{identity}/messages')
    def messages(identity: str, before: str | None = None):
        page=repository.messages(identity,before)
        event=ChatEvent('web','group','',identity,OWNER,'')
        native=['web-'+item['id'][2:] for item in page['items'] if item['role']=='assistant' and item['kind']!='progress']
        links=store.tasks.message_events(event,native)
        hotel=runtime.travel_service.hotel_prices
        for item in page['items']:
            source=links.get('web-'+item['id'][2:]) if item['role']=='assistant' else None
            if source:
                actions=hotel.actions(event,store.tasks.for_event(event,source),source)
                presentation=hotel.presentation(event,source,item['content'])
                if presentation:
                    item['hotel_presentation']=presentation
                    actions=[{k:v for k,v in action.items() if k!='reference_text'} for action in actions]
                if actions:item['hotel_actions']=actions
        return page

    @conversations_api.get('/api/conversations/{identity}/hotel-quotes/{source_event}/original')
    def hotel_original(identity:str,source_event:str,revision:str='',cursor:int=0):
        repository.conversation(identity)
        try:
            return runtime.travel_service.hotel_prices.original(ChatEvent('web','group','',identity,OWNER,''),source_event,revision,cursor=cursor)
        except ValueError as error:raise HTTPException(404,str(error)) from None

    @conversations_api.post('/api/conversations/{identity}/hotel-actions',status_code=202)
    async def hotel_action(identity:str,body:HotelActionInput):
        await asyncio.to_thread(repository.conversation,identity)
        action=body.model_dump(exclude_none=True,exclude={'client_request_id'})
        previous=store.inbox.get(f'web:group:{identity}:{body.client_request_id}')
        if previous:
            import json
            if json.loads(previous['payload_json']).get('hotel_action')!=action:raise WebConflict('相同请求编号对应不同酒店操作。')
            return {'job_id':previous['id'],'status':previous['status']}
        text={'select':'选择当前酒店候选','accept_dates':'使用这组日期','reject_dates':'修改日期'}[body.kind]
        event=ChatEvent('web','group',body.client_request_id,identity,OWNER,text,hotel_action=action)
        try:runtime.travel_service.hotel_prices.validate_action(event,action)
        except ValueError as error:raise HTTPException(409,str(error)) from None
        result=await asyncio.to_thread(repository.accept,identity,text,[],body.client_request_id,hotel_action=action)
        worker.wakeup.set()
        return result

    @conversations_api.post('/api/conversations/{identity}/messages', status_code=202)
    async def send(identity: str, body: MessageInput):
        await asyncio.to_thread(repository.conversation, identity)
        if not body.content.strip() and not body.upload_ids:
            raise HTTPException(422, '请输入问题或选择附件。')
        if len(set(body.upload_ids)) != len(body.upload_ids):
            raise HTTPException(422, '请不要重复附加同一个文件。')
        uploads = [await asyncio.to_thread(repository.upload, item, identity) for item in body.upload_ids]
        if any(item['content_type'].startswith('image/') for item in uploads) and any(not item['content_type'].startswith('image/') for item in uploads):
            raise HTTPException(422, '图片与文档请分成两条消息发送，以便分别识别和导入。')
        try:
            result = await asyncio.to_thread(repository.accept, identity, body.content.strip(), body.upload_ids, body.client_request_id,
                                             priority=request_priority(body.content, bool(body.upload_ids)),quoted_message_id=body.quoted_message_id)
        except WebConflict:
            raise
        except WebReferenceError as error:
            raise HTTPException(422,str(error)) from None
        except ValueError as error:
            raise HTTPException(503, str(error)) from None
        worker.wakeup.set()
        return result

    @operations_api.get('/api/tasks/{identity}')
    def task(identity: int):
        return repository.job(identity)

    @conversations_api.get('/api/conversations/{identity}/rail-results/{result_id}')
    def rail_result(identity:str,result_id:str,page:int=1,period:str='current'):
        try:
            return repository.rail_result(identity,result_id,page=page,period=period)
        except ValueError as error:
            raise HTTPException(422,str(error)) from None

    @conversations_api.post('/api/conversations/{identity}/rail-results/{result_id}/query',status_code=202)
    async def query_rail_result(identity:str,result_id:str,body:RailQueryInput):
        await asyncio.to_thread(repository.conversation,identity)
        previous=store.inbox.get(f'web:group:{identity}:{body.client_request_id}')
        if previous:
            import json
            action=json.loads(previous['payload_json']).get('rail_query',{})
            if action.get('source_result_id')!=result_id or action.get('period')!=body.period:
                raise WebConflict('相同请求编号对应不同的车次查询。')
            return {'job_id':previous['id'],'status':previous['status']}
        values,content=await asyncio.to_thread(repository.rail_query_arguments,identity,result_id,body.period)
        result=await asyncio.to_thread(repository.accept,identity,content,[],body.client_request_id,
            rail_query={'source_result_id':result_id,'period':body.period,'request':values})
        worker.wakeup.set()
        return result

    @conversations_api.get('/api/conversations/{identity}/jobs/{job_id}/diagnostics')
    def diagnostics(identity: str, job_id: int):
        from infrastructure.execution_report import web_report
        repository.conversation(identity)
        job = repository.job(job_id)
        if job['scope_id'] != identity:
            raise LookupError('请求不存在。')
        result = web_report(store.database_path, job)
        result['retrieval_status'] = retrieval_status(store, f'web:{identity}')
        repository.conversation(identity)
        return result

    @conversations_api.get('/api/conversations/{identity}/messages/{message_id}/plan-comparison')
    def plan_comparison(identity:str,message_id:str):
        return repository.plan_comparison(identity,message_id)

    @operations_api.post('/api/tasks/{identity}/cancel')
    def cancel(identity: int):
        row = repository.job(identity)
        cancelled = store.inbox.cancel(row['event_key'], 'web', row['scope_id'], OWNER)
        if not cancelled:
            raise HTTPException(409, '任务已完成或结果正在投递，不能回滚；已创建的提醒请单独取消。')
        return {'status': 'cancelled'}

    @conversations_api.get('/api/conversations/{identity}/context')
    def context(identity: str):
        repository.conversation(identity)
        event = ChatEvent('web', 'group', '', identity, OWNER, '')
        runtime.application.planning_service.ensure_history(event)
        return build_context(identity)

    @conversations_api.post('/api/conversations/{identity}/trips/{trip_id}/fields',status_code=202)
    async def edit_trip_field(identity: str, trip_id: str, body: TripEditInput):
        from tools.trip.constraints import edit_text
        try:
            content=edit_text(body.field,body.value)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        await asyncio.to_thread(repository.conversation,identity)
        data={'trip_id':trip_id,'version':body.version,'draft_id':body.draft_id,'draft_version':body.draft_version,
              'field':body.field,'value':body.value}
        result=await asyncio.to_thread(repository.accept,identity,content,[],body.client_request_id,None,data)
        worker.wakeup.set()
        return result

    @conversations_api.post('/api/conversations/{identity}/confirmations/{confirmation_id}', status_code=202)
    async def confirm(identity: str, confirmation_id: str, body: ConfirmationInput):
        await asyncio.to_thread(repository.conversation, identity)
        # Replay first: a successfully executed confirmation no longer appears in the pending list.
        previous = store.inbox.get(f'web:group:{identity}:{body.client_request_id}')
        if previous:
            import json
            payload = json.loads(previous['payload_json'])
            if payload.get('confirmation') != {'id': confirmation_id, 'version': body.version}:
                raise WebConflict('相同请求编号对应不同的确认。')
            return {'job_id': previous['id'], 'status': previous['status']}
        pending = await asyncio.to_thread(build_context, identity)
        matches = [item for item in pending['confirmations'] if item['id'] == confirmation_id and item['version'] == body.version]
        if matches and not matches[0]['can_confirm']:
            raise HTTPException(409, matches[0]['unavailable_reason'])
        if len(matches) != 1:
            raise WebConflict('预览已变化或过期，请重新核对。')
        result = await asyncio.to_thread(repository.accept, identity, matches[0]['command'], [], body.client_request_id,
                                         {'id': confirmation_id, 'version': body.version})
        worker.wakeup.set()
        return result

    @conversations_api.post('/api/conversations/{identity}/drafts/{draft_id}/actions',status_code=202)
    async def draft_action(identity:str,draft_id:str,body:DraftActionInput):
        data={'id':draft_id,'version':body.version,'action':body.action}
        content='重新核对这份草案' if body.action=='revalidate' else '放弃这份草案'
        try:
            result=await asyncio.to_thread(repository.accept,identity,content,[],body.client_request_id,
                draft_action=data,priority=1 if body.action=='discard' else 0)
        except DraftActionConflict as error:
            return JSONResponse(status_code=409,content={'detail':str(error),'latest_draft':error.latest})
        worker.wakeup.set()
        return result

    @operations_api.get('/api/notifications')
    def notifications():
        return repository.notifications()

    @operations_api.post('/api/notifications/{identity}/read')
    def read_notification(identity: int):
        repository.read_notification(identity)
        return {'status': 'read'}

    @conversations_api.post('/api/uploads', status_code=201)
    async def upload(conversation_id: str = Form(...), file: UploadFile = File(...)):
        await asyncio.to_thread(repository.conversation, conversation_id)
        data = await file.read(MAX_UPLOAD + 1)
        await file.close()
        filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1][:180]
        suffix = Path(filename).suffix.lower()
        if not data or len(data) > MAX_UPLOAD:
            raise HTTPException(413, '文件不能为空，且不能超过 5 MB。')
        if suffix not in DOCUMENTS | IMAGES.keys():
            raise HTTPException(415, '支持 TXT、Markdown、DOCX、XLSX，以及 JPEG、PNG、WebP 图片。')
        def save():
            try:
                if suffix in IMAGES:
                    with Image.open(io.BytesIO(data)) as picture:
                        if picture.format != IMAGES[suffix] or picture.width * picture.height > 25_000_000:
                            raise ValueError('图片格式不匹配或尺寸过大。')
                        picture.verify()
                    content_type = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}[IMAGES[suffix]]
                else:
                    if suffix in {'.docx', '.xlsx'}:
                        validate_office_archive(data)
                        with zipfile.ZipFile(io.BytesIO(data)) as archive:
                            required = 'word/document.xml' if suffix == '.docx' else 'xl/workbook.xml'
                            if required not in archive.namelist():
                                raise ValueError('文件内容与扩展名不匹配。')
                    elif b'\x00' in data:
                        raise ValueError('请上传文本文件，不支持二进制内容。')
                    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            except (ValueError, UnidentifiedImageError, OSError, Image.DecompressionBombError, zipfile.BadZipFile) as error:
                raise HTTPException(415, '文件校验失败，请检查文件格式与内容。') from error
            total = repository.upload_bytes()
            if total + len(data) > 512 * 1024 * 1024:
                raise HTTPException(413, '本地附件容量已达 512 MB，请清理过期资料。')
            identity = uuid.uuid4().hex
            relative = f'inbox-assets/web/{identity}{suffix}'
            destination = repository.root / relative
            temporary = destination.with_suffix('.part')
            with publish_files(store, 'web', conversation_id, (destination, temporary)):
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_bytes(data)
                temporary.replace(destination)
                try:
                    item = repository.save_upload(identity, conversation_id, filename, relative, content_type, data)
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
            return {key: item[key] for key in ('id', 'filename', 'content_type', 'size')}
        return await asyncio.to_thread(save)

    @conversations_api.get('/api/uploads/{identity}')
    def download(identity: str):
        item = repository.upload(identity)
        inline = item['content_type'].startswith('image/')
        return FileResponse(repository.root / item['relative_path'], media_type=item['content_type'],
                            filename=item['filename'], content_disposition_type='inline' if inline else 'attachment')

    @operations_api.get('/api/status')
    def status():
        return {'version': '0.1.0', 'mode': 'local', 'llm_configured': travel_settings.llm_configured,
                'amap_configured': bool(travel_settings.amap_api_key), 'tasks': runtime.supervisor.snapshot(),
                'queue': store.inbox.health(), 'data_isolated': True}

    @app.get('/health/live')
    def live():
        return {'status': 'ok'}

    @app.get('/health/ready')
    def ready():
        try:
            store.inbox.health()
            snapshot = runtime.supervisor.snapshot()
            expected_workers = 5 + int(getattr(store, 'semantic_index', None) is not None)
            healthy = not start_workers or (len(snapshot) == expected_workers and all(item['running'] for item in snapshot.values()))
        except Exception:
            healthy = False
        return JSONResponse({'status': 'ok' if healthy else 'degraded'}, status_code=200 if healthy else 503)

    for router in (conversations_api, settings_api, operations_api):
        app.include_router(router)

    distribution = PROJECT_ROOT / 'frontend' / 'dist'
    if (distribution / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=distribution / 'assets'), name='assets')

    @app.get('/')
    def index():
        path = distribution / 'index.html'
        if not path.is_file():
            return JSONResponse({'detail': '网页尚未构建。请在 frontend 目录执行 npm ci 和 npm run build，或使用开发模式。'}, status_code=503)
        return FileResponse(path)

    return app
