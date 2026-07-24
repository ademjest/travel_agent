from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from chat_transport import storage_scope_id
from document_service import DocumentService
from memory_store import MemoryStore, UploadBindingRedemption


UPLOAD_BINDING_TTL = timedelta(minutes=10)
UPLOAD_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
UPLOAD_CODE_PATTERN = re.compile(r"QG-[A-HJ-NP-Z2-9]{6}", re.IGNORECASE)


@dataclass(frozen=True)
class PrivateUploadResult:
    reply: str
    group_openid: str = ""
    outbox_id: int | None = None


class UploadBindingService:
    def __init__(
            self,
            memory_store: MemoryStore,
            document_service: DocumentService,
            group_allowed: Callable[[str], bool] | None = None,
            code_factory: Callable[[], str] | None = None,
            now_provider: Callable[[], datetime] | None = None):
        self.memory_store = memory_store
        self.document_service = document_service
        self.group_allowed = group_allowed or (lambda group_openid: True)
        self.code_factory = code_factory or self._generate_code
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def issue_binding(
            self,
            group_openid: str,
            issuer_openid: str,
            *,
            event_id: str = "",
            claim_token: str = "") -> str:
        code = self.code_factory().upper()
        now = self.now_provider()
        reply = (
            f"一次性绑定码：{code}\n"
            "请在 10 分钟内私聊机器人，先发送该绑定码，再发送一个 "
            ".docx、.txt、.md 或 .xlsx 文件。收到一次附件消息后绑定即失效。"
        )
        if event_id:
            if not claim_token:
                raise ValueError("Issuing a binding for an event requires a claim")
            return self.memory_store.create_upload_binding_for_event(
                code_hash=self._hash_code(code),
                group_openid=group_openid,
                issuer_openid=issuer_openid,
                expires_at=now + UPLOAD_BINDING_TTL,
                event_id=event_id,
                claim_token=claim_token,
                reply=reply,
                now=now,
            )
        self.memory_store.create_upload_binding(
            code_hash=self._hash_code(code),
            group_openid=group_openid,
            issuer_openid=issuer_openid,
            expires_at=now + UPLOAD_BINDING_TTL,
            now=now,
        )
        return reply

    def handle_private_message(
            self,
            c2c_user_openid: str,
            content: str,
            attachments: list,
            *,
            event_id: str = "",
            claim_token: str = "",
            platform: str = "qq_official",
            reply_to_id: str = "") -> PrivateUploadResult:
        if event_id:
            existing = self.memory_store.list_outbox_for_event(event_id)
            if existing:
                reply = existing[0].payload.get("content", "")
                return PrivateUploadResult(
                    reply=str(reply),
                    outbox_id=existing[0].outbox_id,
                )

        text = (content or "").strip()
        code_match = UPLOAD_CODE_PATTERN.search(text.upper())
        redemption = None
        if code_match:
            code = code_match.group(0).upper()
            redemption = self.memory_store.redeem_upload_binding(
                self._hash_code(code),
                c2c_user_openid,
                now=self.now_provider(),
            )
            error_reply = self._redemption_error(redemption)
            if error_reply:
                return PrivateUploadResult(reply=error_reply)
            if not self.group_allowed(redemption.binding.group_openid):
                reply = (
                    "目标群当前不在机器人允许列表中，请回到群里重新申请。"
                )
                outbox_id = self._commit_binding_reply(
                    binding_id=redemption.binding.binding_id,
                    reply=reply,
                    c2c_user_openid=c2c_user_openid,
                    event_id=event_id,
                    claim_token=claim_token,
                    platform=platform,
                    reply_to_id=reply_to_id,
                )
                return PrivateUploadResult(
                    reply=reply,
                    outbox_id=outbox_id,
                )
            if not attachments:
                return PrivateUploadResult(
                    reply=(
                        "绑定成功。现在请直接发送一个 "
                        ".docx、.txt、.md 或 .xlsx 文件；"
                        "不需要附加文字。"
                    ),
                    group_openid=redemption.binding.group_openid,
                )

        if not attachments:
            return PrivateUploadResult(
                reply=(
                    "请先在目标群中发送“@机器人 上传文档”获取一次性绑定码，"
                    "再私聊发送绑定码和文件。"
                )
                )

        if len(attachments) > 1:
            pending = self.memory_store.get_pending_upload_binding(
                c2c_user_openid,
                now=self.now_provider(),
            )
            reply = (
                "一次只能上传一个旅行文档。请回到目标群重新申请绑定码，"
                "再逐个发送文件。"
            )
            if pending is None:
                return PrivateUploadResult(reply=reply)
            outbox_id = self._commit_binding_reply(
                binding_id=pending.binding_id,
                reply=reply,
                c2c_user_openid=c2c_user_openid,
                event_id=event_id,
                claim_token=claim_token,
                platform=platform,
                reply_to_id=reply_to_id,
            )
            return PrivateUploadResult(
                reply=reply,
                group_openid=pending.group_openid,
                outbox_id=outbox_id,
            )

        pending = self.memory_store.get_pending_upload_binding(
            c2c_user_openid,
            now=self.now_provider(),
        )
        if pending is None:
            return PrivateUploadResult(
                reply=(
                    "当前没有有效的群绑定。请回到目标群发送“@机器人 上传文档”"
                    "重新获取绑定码。"
                )
            )
        if not self.group_allowed(pending.group_openid):
            reply = "目标群当前不在机器人允许列表中，请回到群里重新申请。"
            outbox_id = self._commit_binding_reply(
                binding_id=pending.binding_id,
                reply=reply,
                c2c_user_openid=c2c_user_openid,
                event_id=event_id,
                claim_token=claim_token,
                platform=platform,
                reply_to_id=reply_to_id,
            )
            return PrivateUploadResult(
                reply=reply,
                group_openid=pending.group_openid,
                outbox_id=outbox_id,
            )

        prepared = self.document_service.prepare_attachments(
            list(attachments)
        )
        if not prepared:
            legacy_excel = any(
                Path(str(getattr(item, "filename", "") or "")).suffix.lower()
                == ".xls"
                for item in attachments
            )
            if legacy_excel:
                reply = (
                    "暂不支持旧版 Excel，请另存为 .xlsx 后重新上传。"
                    "本次绑定已失效，请回到目标群重新申请。"
                )
            else:
                reply = (
                    "该附件格式暂不支持。请发送 "
                    ".docx、.txt、.md 或 .xlsx 文件，"
                    "本次绑定已失效，请回到目标群重新申请。"
                )
            outbox_id = self._commit_binding_reply(
                binding_id=pending.binding_id,
                reply=reply,
                c2c_user_openid=c2c_user_openid,
                event_id=event_id,
                claim_token=claim_token,
                platform=platform,
                reply_to_id=reply_to_id,
            )
            return PrivateUploadResult(
                reply=reply,
                group_openid=pending.group_openid,
                outbox_id=outbox_id,
            )

        if not event_id or not claim_token or not reply_to_id:
            raise ValueError("Document upload requires an event claim")

        document = prepared[0]
        reply_lines = [
            (
                f"已保存旅行文档：{document.filename}"
                f"（{len(document.full_text)} 字，{len(document.chunks)} 个片段）。"
            )
        ]
        if document.summary:
            reply_lines.append("已生成长期行程摘要。")
        reply_lines.extend((
            "文档属于群共享长期资料，不受最近 6 轮对话限制。后续提问时会按内容检索相关片段。",
            "本次绑定已失效。",
        ))
        reply = "\n".join(reply_lines)
        outbox_id = self.memory_store.commit_private_document_event(
            event_id=event_id,
            claim_token=claim_token,
            platform=platform,
            binding_id=pending.binding_id,
            group_openid=pending.group_openid,
            document_group_openid=storage_scope_id(
                platform,
                pending.group_openid,
            ),
            uploader_openid=f"c2c:{c2c_user_openid}",
            document=document,
            reply=reply,
            target_user_openid=c2c_user_openid,
            reply_to_id=reply_to_id,
            now=self.now_provider(),
        )
        return PrivateUploadResult(
            reply=reply,
            group_openid=pending.group_openid,
            outbox_id=outbox_id,
        )

    def _commit_binding_reply(
            self,
            *,
            binding_id: int,
            reply: str,
            c2c_user_openid: str,
            event_id: str,
            claim_token: str,
            platform: str,
            reply_to_id: str) -> int | None:
        if event_id and claim_token and reply_to_id:
            return self.memory_store.commit_private_reply_event(
                event_id=event_id,
                claim_token=claim_token,
                platform=platform,
                binding_id=binding_id,
                reply=reply,
                target_user_openid=c2c_user_openid,
                reply_to_id=reply_to_id,
                now=self.now_provider(),
            )
        self.memory_store.consume_upload_binding(
            binding_id,
            now=self.now_provider(),
        )
        return None

    @staticmethod
    def _redemption_error(redemption: UploadBindingRedemption) -> str:
        if redemption.status in {"redeemed", "already_redeemed"}:
            return ""
        if redemption.status == "expired":
            return "绑定码已过期，请回到目标群重新申请。"
        if redemption.status == "used":
            return "绑定码已被使用，请回到目标群重新申请。"
        return "绑定码无效，请检查后重试，或回到目标群重新申请。"

    @staticmethod
    def _generate_code() -> str:
        suffix = "".join(
            secrets.choice(UPLOAD_CODE_ALPHABET)
            for _ in range(6)
        )
        return f"QG-{suffix}"

    @staticmethod
    def _hash_code(code: str) -> str:
        return hashlib.sha256(code.upper().encode("ascii")).hexdigest()
