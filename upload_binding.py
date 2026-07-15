import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from document_service import DocumentService
from memory_store import MemoryStore, UploadBindingRedemption


UPLOAD_BINDING_TTL = timedelta(minutes=10)
UPLOAD_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
UPLOAD_CODE_PATTERN = re.compile(r"QG-[A-HJ-NP-Z2-9]{6}", re.IGNORECASE)


@dataclass(frozen=True)
class PrivateUploadResult:
    reply: str
    group_openid: str = ""


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

    def issue_binding(self, group_openid: str, issuer_openid: str) -> str:
        code = self.code_factory().upper()
        now = self.now_provider()
        self.memory_store.create_upload_binding(
            code_hash=self._hash_code(code),
            group_openid=group_openid,
            issuer_openid=issuer_openid,
            expires_at=now + UPLOAD_BINDING_TTL,
            now=now,
        )
        return (
            f"一次性绑定码：{code}\n"
            "请在 10 分钟内私聊机器人，先发送该绑定码，再发送一个 "
            ".docx、.txt 或 .md 文件。收到一次附件消息后绑定即失效。"
        )

    def handle_private_message(
            self,
            c2c_user_openid: str,
            content: str,
            attachments: list) -> PrivateUploadResult:
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
                self.memory_store.consume_upload_binding(
                    redemption.binding.binding_id,
                    now=self.now_provider(),
                )
                return PrivateUploadResult(
                    reply="目标群当前不在机器人允许列表中，请回到群里重新申请。"
                )
            if not attachments:
                return PrivateUploadResult(
                    reply=(
                        "绑定成功。现在请直接发送一个 .docx、.txt 或 .md 文件；"
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

        pending = self.memory_store.claim_pending_upload_binding(
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
            return PrivateUploadResult(
                reply="目标群当前不在机器人允许列表中，请回到群里重新申请。"
            )

        document_result = self.document_service.ingest_attachments(
            pending.group_openid,
            f"c2c:{c2c_user_openid}",
            list(attachments),
        )
        if not document_result.handled:
            return PrivateUploadResult(
                reply=(
                    "该附件格式暂不支持。请发送 .docx、.txt 或 .md 文件，"
                    "本次绑定已失效，请回到目标群重新申请。"
                ),
                group_openid=pending.group_openid,
            )

        return PrivateUploadResult(
            reply=f"{document_result.reply}\n本次绑定已失效。",
            group_openid=pending.group_openid,
        )

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
