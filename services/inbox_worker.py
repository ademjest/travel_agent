import asyncio
from contextlib import nullcontext, suppress
from datetime import datetime, timezone
import json
import logging
import re

from core.chat_transport import ChatEvent
from core.execution_scope import CURRENT_EXECUTION, ExecutionRevoked, ExecutionScope
from infrastructure.attachment_cache import AttachmentCache
from infrastructure.inbox_repository import JOB_DEADLINE_SECONDS, MAX_JOB_ATTEMPTS
from services.scheduled_query_service import scheduled_query_control
from services.policy_watch_service import watch_control


logger = logging.getLogger(__name__)


class InboxWorker:
    def __init__(self, store, adapter, *, attachment_cache=None, worker_count=2,
                 deadline_seconds=JOB_DEADLINE_SECONDS, capture_deadline_seconds=180):
        self.store = store
        self.adapter = adapter
        self.platform = adapter.platform
        self.cache = attachment_cache or AttachmentCache(store)
        self.adapter.attachment_cache = self.cache
        self.worker_count = worker_count
        self.deadline_seconds = deadline_seconds
        self.capture_deadline_seconds = capture_deadline_seconds
        self.wakeup = asyncio.Event()

    async def submit(self, payload):
        normalized = self.adapter.normalize_for_inbox(payload)
        if normalized is None:
            return {'status': 'ignored'}
        event_key, scope, owner, clean, has_assets = normalized
        text = self.adapter.input_text(clean).strip()
        priority = int(not has_assets and (scheduled_query_control(text) or watch_control(text) or text in
            {'查看任务进度', '查看任务', '取消正在处理的请求', '取消当前任务'} or bool(re.fullmatch(r'取消请求\s+\d+', text))))
        try:
            row = await asyncio.to_thread(self.store.inbox.submit, event_key, self.platform, scope, owner, clean, has_assets, priority=priority)
        except ValueError as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        self.wakeup.set()
        if priority:
            job = await asyncio.to_thread(self.store.inbox.claim, job_id=row['id'], platform=self.platform)
            if job:
                try:
                    result = await self._execute(job)
                    await asyncio.to_thread(self.store.inbox.finish, job, result)
                except Exception as exc:
                    await asyncio.to_thread(self.store.inbox.fail, job, type(exc).__name__)
        return {'status': 'accepted', 'job_id': row['id']}

    async def _reply(self, event_key, scope, owner, payload, text, *, synthetic=False, claim=None):
        await asyncio.to_thread(self._prepare_reply, event_key, scope, owner, payload, text,
                                synthetic=synthetic, claim=claim)

    def _prepare_reply(self, event_key, scope, owner, payload, text, *, synthetic=False, claim=None):
        lifecycle = getattr(self.store, 'web_lifecycle', None) if self.platform == 'web' else None
        with lifecycle.file_lock if lifecycle else nullcontext():
            if self.platform == 'web' and not self.adapter.scope_allowed(scope):
                return
            claim = claim or self.store.begin_event(event_key)
            if claim is None:
                return
            channel = self.adapter.input_channel(payload)
            original = self.adapter.input_text(payload)
            rendered = self.adapter.application.reply_renderer.render(channel, original, text)
            self.store.prepare_event_outbox(event_key, claim.claim_token, self.platform,
                channel, scope, owner, self.adapter.input_reply_id(payload), rendered,
                '[后台任务通知]' if synthetic else original, assistant_text=text)

    async def _control(self, event_key, scope, owner, payload):
        text = self.adapter.input_text(payload).strip()
        if text not in {'查看任务进度', '查看任务', '取消正在处理的请求'} and not text.startswith('取消请求 '):
            return False
        active = await asyncio.to_thread(self.store.inbox.active_for_owner, self.platform, scope, owner)
        active = [row for row in active if row['event_key'] != event_key]
        if '_control_target' in payload:
            selected = [row for row in active if row['event_key'] == payload['_control_target']]
            if not selected:
                await self._reply(event_key, scope, owner, payload, '该请求已取消或已结束，没有继续执行取消操作。')
                return True
            cancelled = await asyncio.to_thread(self.store.inbox.cancel, selected[0]['event_key'], self.platform, scope, owner)
            await self._reply(event_key, scope, owner, payload, '已取消该请求。' if cancelled else '该请求已完成业务处理，不能回滚。')
            return True
        if not active:
            if text.startswith('取消请求 ') or text == '取消正在处理的请求':
                await asyncio.to_thread(self.store.inbox.bind_control_target, event_key, '')
                await self._reply(event_key, scope, owner, payload, '没有找到你尚未完成的后台请求。')
                return True
            return False
        if text in {'查看任务进度', '查看任务'}:
            lines = []
            for row in active[:20]:
                source = json.loads(row['payload_json'])
                summary = ('定时查询 ' + str(source['query_id']) if source.get('post_type') == 'scheduled_query' else
                           self.adapter.input_text(source)[:80] or '[附件消息]')
                state = '处理中' if row['status'] == 'running' else '正在保存附件' if row['capture_state'] in {'pending', 'capturing'} else '等待处理'
                lines.append(f"请求 {row['id']}：{state} — {summary}")
            await self._reply(event_key, scope, owner, payload, '\n'.join(lines) + '\n可发送“取消请求 编号”取消尚未完成的请求。')
            return True
        selected = active
        if text.startswith('取消请求 '):
            key = text.removeprefix('取消请求 ').strip()
            selected = [row for row in active if str(row['id']) == key]
        elif len(active) > 1:
            selected = [row for row in active if row['status'] == 'running']
        if len(selected) != 1:
            await asyncio.to_thread(self.store.inbox.bind_control_target, event_key, '')
            await self._reply(event_key, scope, owner, payload, '请先“查看任务进度”，再按请求编号取消具体请求。')
            return True
        await asyncio.to_thread(self.store.inbox.bind_control_target, event_key, selected[0]['event_key'])
        cancelled = await asyncio.to_thread(self.store.inbox.cancel, selected[0]['event_key'], self.platform, scope, owner)
        await self._reply(event_key, scope, owner, payload,
            '已取消尚未完成的请求，后续不会再写入业务结果；之前已经完成的操作不会回滚。' if cancelled else
            '该请求已完成业务处理或正在投递结果，不能回滚；已创建的提醒请单独取消。')
        return True

    async def _execute(self, job):
        scope = ExecutionScope(str(self.store.database_path.resolve()), job['id'], job['claim_token'])
        token = CURRENT_EXECUTION.set(scope)
        try:
            if job['payload'].get('post_type') == 'scheduled_query':
                return await self.adapter.application.scheduled_query_service.execute_job(job, self.adapter.application.reply_renderer)
            if job['payload'].get('post_type') == 'policy_watch':
                return await self.adapter.application.policy_watch_service.execute_job(job, self.adapter.application.reply_renderer)
            if job.get('priority'):
                payload = job['payload']
                with self.store._connect() as connection:
                    saved = connection.execute('SELECT status, prepared_reply FROM processed_events WHERE event_id=?', (job['event_key'],)).fetchone()
                if saved and saved['status'] == 'completed':
                    return {'status': 'handled'}
                if saved and saved['prepared_reply'] is not None:
                    event = self.adapter.input_event(payload)
                    await self.adapter.application.handle(event)
                    return {'status': 'handled'}
                text = self.adapter.input_text(payload).strip()
                service = (getattr(self.adapter.application, 'policy_watch_service', None) if watch_control(text)
                           else getattr(self.adapter.application, 'scheduled_query_service', None))
                if service is not None and (scheduled_query_control(text) or watch_control(text)):
                    event = self.adapter.input_event(payload)
                    claim = await asyncio.to_thread(self.store.begin_event, job['event_key'])
                    if claim:
                        reply = claim.prepared_reply if claim.prepared_reply is not None else await asyncio.to_thread(service.handle, event, claim)
                        await self._reply(job['event_key'], job['scope_id'], job['owner_id'], payload, reply, claim=claim)
                    return {'status': 'handled'}
                if await self._control(job['event_key'], job['scope_id'], job['owner_id'], payload):
                    return {'status': 'handled'}
            return await self.adapter.handle(job['payload'])
        finally:
            CURRENT_EXECUTION.reset(token)

    async def _renew(self, job):
        while True:
            await asyncio.sleep(20)
            if not await asyncio.to_thread(self.store.inbox.renew, job['id'], job['claim_token']):
                return

    async def _recover_prepared(self, job):
        recovered = await asyncio.to_thread(self.store.inbox.recovery_claim, job)
        if recovered is None:
            return False
        token, event = recovered
        job['claim_token'] = token
        payload = job['payload']
        channel = self.adapter.input_channel(payload)
        if payload.get('post_type') in {'scheduled_query', 'policy_watch'}:
            rendered = self.adapter.application.reply_renderer.render_reminder(job['owner_id'], event['prepared_reply'])
        else:
            rendered = self.adapter.application.reply_renderer.render(channel, event['prepared_memory_content'] or '', event['prepared_reply'])
        await asyncio.to_thread(self.store.prepare_event_outbox, job['event_key'], event['claim_token'], self.platform,
            channel, job['scope_id'], job['owner_id'], self.adapter.input_reply_id(payload), rendered,
            event['prepared_memory_content'], assistant_text=event['prepared_reply'])
        await asyncio.to_thread(self.store.inbox.finish, job, {'status': 'recovered_output'})
        return True

    async def run_once(self, now=None, lane=None):
        job = await asyncio.to_thread(self.store.inbox.claim, now, deadline_seconds=self.deadline_seconds, lane=lane, platform=self.platform)
        if job is None:
            return False
        try:
            if job['payload'].get('post_type') in {'scheduled_query', 'policy_watch'}:
                if not self.adapter.scope_allowed(job['scope_id']):
                    await asyncio.to_thread(self.store.inbox.block, job, 'group_not_allowed')
                    return True
            else:
                self.adapter.normalize_for_inbox(job['payload'])
        except Exception as exc:
            if getattr(exc, 'status_code', None) in {403, 410}:
                await asyncio.to_thread(self.store.inbox.block, job, 'group_not_allowed')
                return True
            raise
        if job['attempts'] > MAX_JOB_ATTEMPTS:
            if not await self._recover_prepared(job):
                await asyncio.to_thread(self.store.inbox.fail, job, 'recovery attempt limit reached')
                await self._reply('inbox-notice:' + job['event_key'], job['scope_id'], job['owner_id'], job['payload'],
                                  '该请求多次恢复仍未完成，请重新发送。', synthetic=True)
            return True
        renewal = asyncio.create_task(self._renew(job))
        execution = asyncio.create_task(self._execute(job))
        try:
            result = await asyncio.wait_for(asyncio.shield(execution), self.deadline_seconds)
            event_status = await asyncio.to_thread(self.store.get_event_status, job['event_key'])
            outbox = await asyncio.to_thread(self.store.list_outbox_for_event, job['event_key'])
            if result.get('status') == 'handled' and event_status == 'processing' and not outbox:
                await asyncio.to_thread(self.store.inbox.fail, job, 'event lease still active', busy=True)
            else:
                await asyncio.to_thread(self.store.inbox.finish, job, result)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self.store.inbox.release, job))
            execution.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await execution
            raise
        except Exception as exc:
            execution.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await execution
            if not await self._recover_prepared(job):
                state = await asyncio.to_thread(self.store.inbox.fail, job, type(exc).__name__)
                if state == 'failed':
                    await self._reply('inbox-notice:' + job['event_key'], job['scope_id'], job['owner_id'], job['payload'],
                        '这次请求在重试后仍未完成，请重新发送。可以用“查看任务进度”检查其他请求。', synthetic=True)
                if state != 'revoked':
                    logger.warning('Inbox attempt failed: job_id=%s error_type=%s state=%s', job['id'], type(exc).__name__, state)
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        await self.adapter.application.outbox_worker.dispatch_due_once(now)
        return True

    async def capture_once(self):
        job = await asyncio.to_thread(self.store.inbox.claim_capture, platform=self.platform)
        if not job:
            return False
        async def renew():
            while True:
                await asyncio.sleep(20)
                if not await asyncio.to_thread(self.store.inbox.renew_capture, job):
                    return
        renewal = asyncio.create_task(renew())
        capture = None
        try:
            self.adapter.normalize_for_inbox(job['payload'])
            async def do_capture():
                event = await self.adapter.event_for_capture(job['payload'])
                return await asyncio.to_thread(self.cache.capture, job, event)
            capture = asyncio.create_task(do_capture())
            assets = await asyncio.wait_for(asyncio.shield(capture), self.capture_deadline_seconds)
            await asyncio.to_thread(self.store.inbox.finish_capture, job, assets)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self.store.inbox.release_capture, job))
            raise
        except Exception as exc:
            if getattr(exc, 'status_code', None) in {403, 410}:
                await asyncio.to_thread(self.store.inbox.block, job, 'group_not_allowed')
                return True
            # NapCat's UUID-only file message may precede a resolvable group_upload notice.
            if getattr(exc, 'status_code', None) == 502 and job['payload'].get('post_type') == 'message':
                await asyncio.to_thread(self.store.inbox.finish_capture, job, (), deferred=True)
            else:
                terminal = await asyncio.to_thread(self.store.inbox.fail_capture, job, type(exc).__name__)
                if terminal and await self._capture_failure_is_actionable(job):
                    await self._reply('inbox-notice:' + job['event_key'], job['scope_id'], job['owner_id'], job['payload'],
                        '附件未能持久保存，下载链接可能已失效，请重新上传。', synthetic=True)
        finally:
            if capture is not None and not capture.done():
                capture.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await capture
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        return True

    async def _capture_failure_is_actionable(self, job):
        return await self.adapter.capture_failure_actionable(job['payload'])

    async def _loop(self, operation):
        while True:
            if not await operation():
                self.wakeup.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)

    async def run(self):
        tasks = [asyncio.create_task(self._loop(lambda: self.run_once(lane='normal'))) for _ in range(self.worker_count)]
        tasks.append(asyncio.create_task(self._loop(lambda: self.run_once(lane='scheduled'))))
        tasks.append(asyncio.create_task(self._loop(self.capture_once)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
