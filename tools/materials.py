"""Document import, optional summary, and general image facts share one owner."""
from __future__ import annotations
from infrastructure.skill_manager import skill_text, skill_request

import base64
import hashlib
import os
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import requests

from core.context_contract import response_json, response_text
from core.model_budget import model_call_context
from core.web_lifecycle import publish_files
from infrastructure.attachment_cache import read_cached_attachment
from infrastructure.document_formats import SUPPORTED_EXTENSIONS, LEGACY_EXCEL_EXTENSION, extract_text, chunk_text
from infrastructure.image_inputs import CONTENT_TYPE_EXTENSIONS, download_image, validate_image
from infrastructure.secure_download import download_https, resolve_host

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
DOCUMENT_TIMEOUT = 20
MAX_DOCUMENT_SUMMARY_INPUT_CHARS = 20000


@dataclass(frozen=True)
class DocumentIngestResult:
    handled: bool
    reply: str = ""
    memory_content: str = ""


from infrastructure.document_repository import PreparedDocument


class MaterialsTool:
    def __init__(self, memory_store, summarizer=None, session=None, resolver=resolve_host,
                 *, client=None, model='', downloader=None):
        self.memory_store = memory_store
        self.client, self.model = client, model
        self.summarizer = summarizer or (self.summarize_document if client is not None else None)
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.resolver = resolver
        self.downloader = downloader or self._download_image

    _extract_text = staticmethod(extract_text)
    _chunk_text = staticmethod(chunk_text)

    def _download_image(self, attachment):
        return download_image(attachment, session=self.session,
                              database_path=self.memory_store.database_path, resolver=self.resolver)

    def _prepare_document(self, attachment):
        filename = str(getattr(attachment, 'filename', '') or '')
        data = self._download_attachment(attachment)
        text = extract_text(filename, data)
        return PreparedDocument(filename, hashlib.sha256(data).hexdigest(), text, tuple(chunk_text(text)), '')

    @staticmethod
    def is_document_attachment(attachment: object) -> bool:
        filename = str(getattr(attachment, "filename", "") or "")
        return Path(filename).suffix.lower() in (
            SUPPORTED_EXTENSIONS | {".doc", LEGACY_EXCEL_EXTENSION}
        )

    def prepare_attachments(
            self,
            attachments: list) -> tuple[PreparedDocument, ...]:
        prepared = []
        for attachment in attachments:
            filename = str(getattr(attachment, "filename", "") or "")
            if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            document = self._prepare_document(attachment)
            if self.summarizer:
                try:
                    document = replace(document, summary=self.summarizer(filename, document.full_text))
                except Exception:
                    pass
            prepared.append(document)
        return tuple(prepared)

    def ingest_attachments(
            self,
            group_openid: str,
            member_openid: str,
            attachments: list) -> DocumentIngestResult:
        document_attachments = []
        legacy_documents = []
        legacy_excel_documents = []

        for attachment in attachments:
            filename = str(getattr(attachment, "filename", "") or "")
            extension = Path(filename).suffix.lower()
            if extension in SUPPORTED_EXTENSIONS:
                document_attachments.append(attachment)
            elif extension == ".doc":
                legacy_documents.append(filename)
            elif extension == LEGACY_EXCEL_EXTENSION:
                legacy_excel_documents.append(filename)

        if (
                not document_attachments
                and not legacy_documents
                and not legacy_excel_documents):
            return DocumentIngestResult(handled=False)

        messages = []
        memory_names = []

        for filename in legacy_documents:
            memory_names.append(filename)
            messages.append(
                f"暂不支持旧版 Word 文件 {filename}，请转换为 .docx 后重新上传。"
            )

        for filename in legacy_excel_documents:
            memory_names.append(filename)
            messages.append(
                f"暂不支持旧版 Excel 文件 {filename}，"
                "请另存为 .xlsx 后重新上传。"
            )

        for attachment in document_attachments:
            filename = str(attachment.filename)
            try:
                prepared = self._prepare_document(attachment)
                text, chunks, digest = prepared.full_text, prepared.chunks, prepared.sha256
                stored = self.memory_store.documents.add_document(
                    group_openid=group_openid,
                    uploader_openid=member_openid,
                    filename=filename,
                    sha256=digest,
                    full_text=text,
                    chunks=chunks,
                )
                memory_names.append(filename)
                if stored.is_new:
                    messages.append(
                        f"已保存旅行文档：{filename}（{len(text)} 字，{len(chunks)} 个片段）。"
                    )
                    if self.summarizer:
                        try:
                            summary = self.summarizer(filename, text)
                            if summary:
                                self.memory_store.documents.update_document_summary(
                                    stored.document_id,
                                    summary,
                                )
                                messages.append("已生成长期行程摘要。")
                        except Exception:
                            messages.append(
                                "自动摘要失败，但文档原文和分块已正常保存。"
                            )
                else:
                    messages.append(f"该文档已保存过：{stored.filename}。")
            except Exception as exc:
                messages.append(f"处理文档 {filename} 失败：{exc}")

        messages.append(
            "文档属于群共享长期资料，不受最近 6 轮对话限制。后续提问时会按内容检索相关片段。"
        )
        return DocumentIngestResult(
            handled=True,
            reply="\n".join(messages),
            memory_content="上传旅行文档：" + "、".join(memory_names),
        )

    def _download_attachment(self, attachment) -> bytes:
        if getattr(attachment, 'local_path', ''):
            return read_cached_attachment(attachment, self.memory_store.database_path, max_bytes=MAX_DOCUMENT_BYTES)
        url = str(getattr(attachment, "url", "") or "")
        declared_size = int(getattr(attachment, "size", 0) or 0)
        data, unused_content_type = download_https(
            self.session,
            url,
            max_bytes=MAX_DOCUMENT_BYTES,
            timeout=DOCUMENT_TIMEOUT,
            declared_size=declared_size,
            resolver=self.resolver,
            size_error="文件超过 5 MB 限制",
        )
        return data


    @skill_request
    def summarize_document(self, filename: str, text: str) -> str:
        source = text[:MAX_DOCUMENT_SUMMARY_INPUT_CHARS]
        with model_call_context('document_summary', prompt_version='document-summary-v1', stage_id='materials.summary'):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                {
                    "role": "system",
                    "content": (
                        skill_text('materials.summary', 'system')
                    ),
                },
                {
                    "role": "user",
                    "content": (skill_text('materials.summary', 'feedback') + f'{filename}' + skill_text('materials.summary', 'feedback_2') + f'{len(text)}' + skill_text('materials.summary', 'feedback_3') + f'{len(source)}' + skill_text('materials.summary', 'feedback_4') + f'{source}'),
                },
                ],
            )
        return response_text(response).strip()

    @skill_request
    def analyze(self, event, attachments):
        if len(attachments) != 1:
            raise ValueError('请一次发送一张需要识别的旅行图片。')
        existing = self.memory_store.media.get_for_event(event)
        if existing:
            return existing
        if self.client is None:
            raise ValueError('图片问答需要配置多模态模型。你也可以直接用文字描述旅行问题。')
        data, content_type = self.downloader(attachments[0])
        validate_image(data, content_type)
        with model_call_context('image_extract', prompt_version='image-context-v1', stage_id='materials.image'):
            response = self.client.chat.completions.create(model=self.model, response_format={'type': 'json_object'},
                messages=[
                {'role': 'system', 'content': (
                    skill_text('materials.image', 'system'))},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': event.content or skill_text('materials.image', 'image_instruction')},
                    {'type': 'image_url', 'image_url': {'url': f'data:{content_type};base64,' + base64.b64encode(data).decode()}},
                ]},
                ])
        result = response_json(response)
        if (not isinstance(result, dict) or type(result.get('uncertain')) is not bool
                or not isinstance(result.get('facts'), str) or not isinstance(result.get('answer'), str)
                or not result['answer'].strip() or len(result['facts']) > 5000 or len(result['answer']) > 3000):
            raise ValueError('图片识别结果不完整，请重试或补充文字说明。')
        result = {key: result[key] for key in ('facts', 'answer', 'uncertain')}
        digest = hashlib.sha256(data).hexdigest()
        owner_scope = hashlib.sha256(f'{event.platform}:{event.storage_scope_id}:{event.sender_id}'.encode()).hexdigest()[:24]
        root = self.memory_store.database_path.parent / 'media' / owner_scope
        extension = CONTENT_TYPE_EXTENSIONS[content_type]
        path = root / (digest + extension)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
        with publish_files(self.memory_store, event.platform, event.scope_id, (path, temporary)):
            root.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                try:
                    temporary.write_bytes(data)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            return self.memory_store.media.save(event, digest, str(path.resolve()), content_type, self.model, result)
