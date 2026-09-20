from core.web_lifecycle import publish_files
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import uuid

import requests

from infrastructure.secure_download import download_https


MAX_ASSET_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
DOCUMENT_EXTENSIONS = {'.docx', '.txt', '.md', '.xlsx'}


def read_cached_attachment(attachment, database_path, *, max_bytes=MAX_ASSET_BYTES):
    if not getattr(attachment, 'local_path', ''):
        return None
    root = (Path(database_path).parent / 'inbox-assets').resolve()
    path = Path(attachment.local_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('本地附件缓存不可用，请重新上传。')
    if path.stat().st_size > max_bytes:
        raise ValueError('附件超过大小限制。')
    with path.open('rb') as stream:
        data = stream.read(max_bytes+1)
    if len(data) > max_bytes or hashlib.sha256(data).hexdigest() != attachment.local_sha256:
        raise ValueError('附件缓存校验失败，请重新上传。')
    return data


class AttachmentCache:
    def __init__(self, store, downloader=None):
        self.store = store
        self.root = store.database_path.parent / 'inbox-assets'
        self.downloader = downloader or self._download

    @staticmethod
    def _download(attachment):
        with requests.Session() as session:
            session.trust_env = False
            return download_https(session, attachment.url, max_bytes=MAX_ASSET_BYTES,
                declared_size=attachment.size, timeout=(10, 20), deadline_seconds=45)

    def capture(self, job, event):
        if len(event.attachments) > 8:
            raise ValueError('一次最多处理 8 个附件。')
        existing = {row['attachment_index']: row for row in self.store.inbox.assets(event.event_key)}
        assets = []
        for index, attachment in enumerate(event.attachments):
            if not self.store.inbox.capture_is_current(job):
                raise ValueError('附件任务已取消或处理权已变化。')
            extension = Path(attachment.filename).suffix.lower()
            if (extension not in DOCUMENT_EXTENSIONS and not attachment.content_type.startswith('image/')
                    and extension not in {'.jpg', '.jpeg', '.png', '.webp'}):
                continue
            old = existing.get(index)
            if old:
                cached = replace(attachment, local_path=old['file_path'], local_sha256=old['sha256'])
                try:
                    read_cached_attachment(cached, self.store.database_path)
                    continue
                except ValueError:
                    pass
            data, content_type = self.downloader(attachment)
            if not self.store.inbox.capture_is_current(job):
                raise ValueError('附件任务已取消或处理权已变化。')
            if len(data) > MAX_ASSET_BYTES:
                raise ValueError('附件超过 5 MB 限制。')
            if attachment.content_type.startswith('image/') and content_type not in IMAGE_TYPES:
                raise ValueError('图片格式必须是 JPEG、PNG 或 WebP。')
            digest = hashlib.sha256(data).hexdigest()
            directory = self.root / hashlib.sha256(event.event_key.encode()).hexdigest()[:24]
            destination = directory / f'{index}-{digest}.asset'
            temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.part')
            with publish_files(self.store, event.platform, event.scope_id, (destination, temporary)):
                directory.mkdir(parents=True, exist_ok=True)
                try:
                    with temporary.open('wb') as stream:
                        stream.write(data)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
                asset = {'index': index, 'path': str(destination.resolve()), 'content_type': content_type,
                         'sha256': digest, 'size': len(data)}
                if not self.store.inbox.record_asset(job, asset):
                    raise ValueError('附件处理权已变化，停止保存缓存。')
                assets.append(asset)
        return assets

    def hydrate(self, event):
        rows = {row['attachment_index']: row for row in self.store.inbox.assets(event.event_key)}
        attachments = []
        for index, attachment in enumerate(event.attachments):
            row = rows.get(index)
            if row:
                attachment = replace(attachment, local_path=row['file_path'], local_sha256=row['sha256'],
                                     content_type=row['content_type'], size=row['byte_size'])
            attachments.append(attachment)
        return replace(event, attachments=tuple(attachments))

    def captured(self, event_key):
        row = self.store.inbox.get(event_key)
        return bool(row and row['capture_state'] == 'ready')
