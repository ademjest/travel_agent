from datetime import datetime

from core.chat_transport import ChatAttachment, ChatEvent
from infrastructure.web_repository import OWNER


class WebAdapter:
    platform = 'web'

    def __init__(self, application, repository, *, context_builder):
        self.application = application
        self.repository = repository
        self.context_builder = context_builder
        self.attachment_cache = None

    def scope_allowed(self, identity):
        return self.repository.allows(identity)

    @staticmethod
    def input_text(payload):
        return payload.get('content', '')

    @staticmethod
    def input_channel(payload):
        return 'group'

    @staticmethod
    def input_reply_id(payload):
        return payload.get('request_id', '')

    def normalize_for_inbox(self, payload):
        self.repository.conversation(payload['conversation_id'])
        if payload.get('version') != 1:
            raise ValueError('不支持的网页任务版本。')
        event = self.input_event(payload)
        return event.event_key, event.scope_id, event.sender_id, payload, False

    def input_event(self, payload):
        attachments = []
        for identity in payload.get('upload_ids', []):
            item = self.repository.upload(identity, payload['conversation_id'])
            attachments.append(ChatAttachment(item['filename'], '', item['content_type'], item['size'],
                               str((self.repository.root / item['relative_path']).resolve()), item['sha256']))
        return ChatEvent('web', 'group', payload['request_id'], payload['conversation_id'], OWNER,
                         payload.get('content', ''), reply_to_id=payload.get('reply_to_id',''), attachments=tuple(attachments), occurred_at=datetime.fromisoformat(payload['time']),
                         trip_edit=payload.get('trip_edit'),rail_query=payload.get('rail_query'),
                         message_reference=payload.get('message_reference'),draft_action=payload.get('draft_action'),
                         confirmation=payload.get('confirmation'),hotel_action=payload.get('hotel_action'))

    async def handle(self, payload):
        event = self.input_event(payload)
        confirmation = payload.get('confirmation')
        if confirmation:
            with self.repository.store._connect() as db:
                cached=db.execute('SELECT prepared_reply FROM processed_events WHERE event_id=?',(event.event_key,)).fetchone()
            if cached and cached[0] is not None:
                await self.application.handle(event)
                return {'status':'handled'}
            tasks = self.repository.store.tasks.recent('web', event.storage_scope_id, OWNER)
            matches = [t for t in tasks if t.task_id == confirmation['id'] and t.version == confirmation['version']
                       and t.status == 'collecting' and 'confirmation' in t.missing_slots]
            previews = self.context_builder(event.scope_id)['confirmations']
            can_confirm = any(p['id'] == confirmation['id'] and p['version'] == confirmation['version']
                              and p['can_confirm'] for p in previews)
            if len(matches) != 1 or not can_confirm:
                # Complete the request with an explanation without invoking any business mutation.
                claim = self.repository.store.delivery.begin_event(event.event_key)
                if claim:
                    text = '该预览已变化或过期，未执行修改。请查看最新行程并重新核对。'
                    self.repository.store.delivery.prepare_event_outbox(event.event_key, claim.claim_token, 'web',
                        'group', event.scope_id, OWNER, event.event_id, {'text': text, 'kind': 'reply'},
                        payload['content'], assistant_text=text)
                return {'status': 'handled'}
        await self.application.handle(event)
        return {'status': 'handled'}

    async def event_for_capture(self, payload):
        return self.input_event(payload)

    async def capture_failure_actionable(self, payload):
        return True
