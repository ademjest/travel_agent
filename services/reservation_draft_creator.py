from __future__ import annotations

import logging
from typing import Sequence

from core.chat_transport import ChatAttachment, storage_scope_id


logger = logging.getLogger(__name__)


class ReservationDraftCreator:
    def __init__(self, image_service, reservation_service):
        self.image_service = image_service
        self.reservation_service = reservation_service

    def create(
            self,
            *,
            platform: str,
            group_id: str,
            creator_id: str,
            event_id: str,
            attachments: Sequence[ChatAttachment],
            attachment_index: int) -> str:
        images = tuple(
            attachment
            for attachment in attachments
            if self.image_service.is_supported_attachment(attachment)
        )
        if not images:
            raise ValueError("当前消息没有预约图片，请先发送一张攻略图片")
        if len(images) > 1:
            raise ValueError("一次只能识别一张预约图片，请逐张发送")
        if attachment_index != 1:
            raise ValueError("attachment_index 必须是当前唯一图片的序号 1")

        try:
            result = self.image_service.process_attachment(
                storage_scope_id=storage_scope_id(platform, group_id),
                platform=platform,
                group_id=group_id,
                uploader_id=creator_id,
                attachment=images[0],
            )
        except ValueError:
            raise
        except Exception:
            logger.exception("Reservation image processing failed")
            raise ValueError(
                "图片下载或识别准备失败，请稍后重新发送"
            ) from None

        extraction_items = (
            result.extraction.items
            if result.extraction is not None
            else ()
        )
        plan = self.reservation_service.create_draft(
            result.image,
            extraction_items,
            source_event_id=event_id,
            creator_id=creator_id,
        )
        reply = self.reservation_service.format_draft(plan)
        if result.extraction is None:
            reply = (
                "图片已保存，但自动识别失败，已转为全手动草稿。\n"
                + reply
            )
        self.reservation_service.finish_workflow(
            platform,
            group_id,
            creator_id,
        )
        return reply
