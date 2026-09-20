import asyncio
from contextlib import asynccontextmanager, nullcontext
import io
import mimetypes
from pathlib import Path
import secrets
import uuid
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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
from infrastructure.web_repository import OWNER, WebConflict, WebRepository
from services.document_service import DocumentService
from services.inbox_worker import InboxWorker
from services.conversation_deletion import ConversationDeletionService
from infrastructure.env_settings_store import EnvSettingsStore, SettingsConflict
from infrastructure.preference_repository import PreferenceConflict
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


class TitleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=80)


class ConfirmationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


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
    adapter = WebAdapter(runtime.application, repository)
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

    @app.get('/api/bootstrap')
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

    @app.get('/api/conversations')
    def conversations():
        return repository.conversations()

    def service_settings():
        nonlocal settings_store
        if settings_store is None:
            settings_store = EnvSettingsStore(PROJECT_ROOT)
        return settings_store

    @app.get('/api/settings/services')
    def get_service_settings():
        try:
            return service_settings().status()
        except Exception:
            raise HTTPException(400, '配置读取失败，请检查文件格式与权限。') from None

    @app.patch('/api/settings/services')
    async def save_service_settings(request: Request):
        # Deliberately no Pydantic input echo in validation errors on credential routes.
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'version', 'changes'}:
                raise ValueError()
            return await asyncio.to_thread(service_settings().save, body['version'], body['changes'])
        except SettingsConflict:
            raise HTTPException(409, '配置已变化或正在保存，请刷新后重试。') from None
        except Exception:
            raise HTTPException(400, '保存失败：检查字段、HTTPS 地址、端点更换时的新 Key、启动环境覆盖、Git 忽略及文件权限。原有配置未被替换。') from None

    @app.post('/api/settings/services/test')
    async def test_service_settings(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'service'} or body['service'] not in ('search', 'llm'):
                raise ValueError()
            return await asyncio.to_thread(service_settings().test, body['service'])
        except Exception:
            raise HTTPException(400, '连接测试未通过：请检查已保存的配置、网络和服务额度。未回传供应商错误正文。') from None

    def preference_event():
        return ChatEvent('web', 'group', 'preferences-settings', '', OWNER, '')

    @app.get('/api/preferences')
    def get_preferences():
        return store.preferences.snapshot(preference_event())

    @app.patch('/api/preferences')
    async def edit_preferences(request: Request):
        try:
            body = await request.json()
            if (not isinstance(body, dict) or set(body) - {'version', 'values', 'enabled', 'suggestions', 'clear'}
                    or 'version' not in body or type(body.get('clear', False)) is not bool):
                raise ValueError('偏好请求字段无效。')
            return await asyncio.to_thread(store.preferences.update, preference_event(), body['version'], body.get('values', {}),
                enabled=body.get('enabled'), suggestions=body.get('suggestions'), clear=body.get('clear', False))
        except PreferenceConflict:
            raise HTTPException(409, '偏好已经变化，请刷新后重试。') from None
        except (ValueError, TypeError):
            raise HTTPException(400, '偏好字段无效，请检查字段、单位和版本。') from None

    @app.get('/api/conversations/{identity}/research')
    def research_results(identity: str):
        repository.conversation(identity)
        event = ChatEvent('web', 'group', '', identity, OWNER, '')
        # No full source bodies in progress responses.
        rows = store.research.latest(event)
        return [{'stage': row['stage'], 'updated_at': row['updated_at'], 'report': row.get('report', ''),
                 'source_count': len(row.get('sources', []))} for row in rows]

    @app.post('/api/conversations', status_code=201)
    def create_conversation():
        return repository.create_conversation()

    @app.patch('/api/conversations/{identity}')
    def rename(identity: str, body: TitleInput):
        if not body.title.strip():
            raise HTTPException(422, '标题不能为空。')
        return repository.rename(identity, body.title.strip())

    @app.get('/api/conversations/{identity}/deletion-summary')
    def deletion_summary(identity: str):
        return deletion.summary(identity)

    @app.delete('/api/conversations/{identity}', status_code=202)
    async def delete_conversation(identity: str):
        result = await asyncio.to_thread(deletion.request, identity)
        deletion.wakeup.set()
        return result

    @app.get('/api/conversation-deletions')
    def deletion_jobs():
        return deletion.list_jobs()

    @app.get('/api/conversation-deletions/{identity}')
    def deletion_status(identity: str):
        return deletion.status(identity)

    @app.post('/api/conversation-deletions/{identity}/retry', status_code=202)
    async def retry_deletion(identity: str):
        result = await asyncio.to_thread(deletion.retry, identity)
        deletion.wakeup.set()
        return result

    @app.get('/api/conversations/{identity}/messages')
    def messages(identity: str, before: str | None = None):
        return repository.messages(identity, before)

    @app.post('/api/conversations/{identity}/messages', status_code=202)
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
            result = await asyncio.to_thread(repository.accept, identity, body.content.strip(), body.upload_ids, body.client_request_id)
        except WebConflict:
            raise
        except ValueError as error:
            raise HTTPException(503, str(error)) from None
        worker.wakeup.set()
        return result

    @app.get('/api/tasks/{identity}')
    def task(identity: int):
        return repository.job(identity)

    @app.post('/api/tasks/{identity}/cancel')
    def cancel(identity: int):
        row = repository.job(identity)
        cancelled = store.inbox.cancel(row['event_key'], 'web', row['scope_id'], OWNER)
        if not cancelled:
            raise HTTPException(409, '任务已完成或结果正在投递，不能回滚；已创建的提醒请单独取消。')
        return {'status': 'cancelled'}

    @app.get('/api/conversations/{identity}/context')
    def context(identity: str):
        return repository.context(identity)

    @app.post('/api/conversations/{identity}/confirmations/{confirmation_id}', status_code=202)
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
        pending = await asyncio.to_thread(repository.context, identity)
        matches = [item for item in pending['confirmations'] if item['id'] == confirmation_id and item['version'] == body.version]
        if len(matches) != 1:
            raise WebConflict('预览已变化或过期，请重新核对。')
        result = await asyncio.to_thread(repository.accept, identity, matches[0]['command'], [], body.client_request_id,
                                         {'id': confirmation_id, 'version': body.version})
        worker.wakeup.set()
        return result

    @app.get('/api/notifications')
    def notifications():
        return repository.notifications()

    @app.post('/api/notifications/{identity}/read')
    def read_notification(identity: int):
        repository.read_notification(identity)
        return {'status': 'read'}

    @app.post('/api/uploads', status_code=201)
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
                        DocumentService._validate_office_archive(data)
                        with zipfile.ZipFile(io.BytesIO(data)) as archive:
                            required = 'word/document.xml' if suffix == '.docx' else 'xl/workbook.xml'
                            if required not in archive.namelist():
                                raise ValueError('文件内容与扩展名不匹配。')
                    elif b'\x00' in data:
                        raise ValueError('请上传文本文件，不支持二进制内容。')
                    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            except (ValueError, UnidentifiedImageError, OSError, Image.DecompressionBombError, zipfile.BadZipFile) as error:
                raise HTTPException(415, '文件校验失败，请检查文件格式与内容。') from error
            with store._connect() as db:
                total = db.execute('SELECT coalesce(sum(size),0) FROM web_uploads').fetchone()[0]
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

    @app.get('/api/uploads/{identity}')
    def download(identity: str):
        item = repository.upload(identity)
        inline = item['content_type'].startswith('image/')
        return FileResponse(repository.root / item['relative_path'], media_type=item['content_type'],
                            filename=item['filename'], content_disposition_type='inline' if inline else 'attachment')

    @app.get('/api/status')
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
            healthy = not start_workers or (len(snapshot) == 5 and all(item['running'] for item in snapshot.values()))
        except Exception:
            healthy = False
        return JSONResponse({'status': 'ok' if healthy else 'degraded'}, status_code=200 if healthy else 503)

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
