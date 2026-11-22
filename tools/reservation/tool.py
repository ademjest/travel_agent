"""Reservation workflow owner: image facts, draft, edits, confirmation and cancellation."""
from __future__ import annotations
from infrastructure.skill_manager import skill_text, skill_request
import base64
import hashlib
import json
import logging
import os
import requests
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal, Mapping, Sequence
from core.chat_transport import storage_scope_id, ChatAttachment
from core.event_idempotency import event_operation_key
from core.time_input import BEIJING as BEIJING_TZ, ABSOLUTE_TIME_FORMAT, parse_beijing_datetime_list
from tools.reservation.dates import (
    AdvanceUnit,
    ReservationExtractionItem,
    VisitDateResolution,
    normalize_extraction_item,
    calculate_booking_date,
    build_reminder_occurrences,
    ItineraryResolver,
    ReservationItineraryResolver,
)
from core.web_lifecycle import publish_files
from pathlib import Path
from openai import OpenAI
from core.data_paths import image_root as default_image_root
from core.model_budget import model_call_context
from core.context_contract import response_text
from infrastructure.memory_store import MemoryStore
from infrastructure.reservation_repository import ReservationImageRecord
from infrastructure.secure_download import resolve_host
from infrastructure.image_inputs import CONTENT_TYPE_EXTENSIONS, download_image, is_supported_attachment
from core.execution_scope import ensure_execution_active
from core.tool_result import AgentToolContext, ToolResult
from core.tool_result import RESERVATION_TOOL_NAMES


MAX_TOOL_TEXT_CHARS = 500


def _text(arguments: Mapping[str, object], name: str) -> str:
    value = str(arguments.get(name) or "").strip()
    if not value:
        raise ValueError(f"缺少参数 {name}")
    if len(value) > MAX_TOOL_TEXT_CHARS:
        raise ValueError(f"参数 {name} 过长")
    return value


def _positive_int(arguments: Mapping[str, object], name: str) -> int:
    try:
        value = int(arguments.get(name))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {name} 必须是整数") from exc
    if value < 1:
        raise ValueError(f"参数 {name} 必须大于等于 1")
    return value


def _times(arguments: Mapping[str, object]):
    raw = arguments.get("times")
    if not isinstance(raw, list) or not raw or len(raw) > 8:
        raise ValueError("times 必须包含 1 到 8 个完整北京时间")
    return parse_beijing_datetime_list(
        ",".join(str(value).strip() for value in raw)
    )



def create_reservation_from_attachment(
        service, image_service, *, platform: str, group_id: str, creator_id: str,
        event_id: str, attachments: Sequence[ChatAttachment], attachment_index: int) -> str:
    images = tuple((attachment for attachment in attachments if image_service.is_supported_attachment(attachment)))
    if not images:
        raise ValueError('当前消息没有预约图片，请先发送一张攻略图片')
    if len(images) > 1:
        raise ValueError('一次只能识别一张预约图片，请逐张发送')
    if attachment_index != 1:
        raise ValueError('attachment_index 必须是当前唯一图片的序号 1')
    try:
        result = image_service.process_attachment(
            storage_scope_id=storage_scope_id(platform, group_id), platform=platform,
            group_id=group_id, uploader_id=creator_id, attachment=images[0])
    except ValueError:
        raise
    except Exception:
        logger.exception('Reservation image processing failed')
        raise ValueError('图片下载或识别准备失败，请稍后重新发送') from None
    extraction_items = result.extraction.items if result.extraction is not None else ()
    plan = service.create_draft(result.image, extraction_items, source_event_id=event_id, creator_id=creator_id)
    reply = service.format_draft(plan)
    if result.extraction is None:
        reply = '图片已保存，但自动识别失败，已转为全手动草稿。\n' + reply
    service.finish_workflow(platform, group_id, creator_id)
    return reply


def execute_reservation(
        service, create_draft, name: str, arguments: Mapping[str, object], context: AgentToolContext) -> str:
    ensure_execution_active()
    if name not in RESERVATION_TOOL_NAMES:
        raise ValueError(f'未知预约工具 {name}')
    normalized_arguments = dict(arguments)
    operation_key = event_operation_key(context.event_id, name, normalized_arguments)
    cacheable = name not in {'list_reservation_plans', 'refresh_reservation_plan'}
    cached = service.store.delivery.get_event_tool_result(operation_key) if cacheable else None
    if cached is not None:
        return cached
    result = _dispatch_reservation(service, create_draft, name, normalized_arguments, context, operation_key)
    if not cacheable:
        return result
    return service.store.delivery.save_event_tool_result(operation_key, context.event_id, name, normalized_arguments, result)


def _refresh_plan(service, arguments, context):
    platform = context.platform
    group_id = context.group_id
    creator_id = context.creator_id
    result = service.refresh_plan(platform, group_id, creator_id, _text(arguments, 'plan_code'))
    prefix = f'已按最新行程刷新 {result.updated_count} 个项目。\n' if result.updated_count else '未找到可自动补齐的新日期。\n'
    return prefix + service.format_draft(result.plan)

def _draft_changes(arguments, *, subject=''):
    visit_date_text = str(arguments.get('visit_date') or '').strip()
    requires = arguments.get('requires_reservation')
    if requires is not None and not isinstance(requires, bool):
        raise ValueError('requires_reservation 必须是布尔值')
    visit_status = str(arguments.get('visit_status') or '').strip()
    if visit_status and visit_status not in {'visit', 'skip'}:
        raise ValueError('visit_status 必须是 visit 或 skip')
    if not visit_date_text and requires is None and not visit_status:
        raise ValueError(f'景点 {subject} 没有提供修改内容' if subject else '至少需要提供一项草稿修改')
    return {'visit_date': date.fromisoformat(visit_date_text) if visit_date_text else None,
            'requires_reservation': requires, 'visit_status': visit_status or None}


def _edit_draft_item(service, arguments, context):
    plan = service.update_draft_item(
        platform=context.platform, group_id=context.group_id, creator_id=context.creator_id,
        plan_code=_text(arguments, 'plan_code'), item_index=_positive_int(arguments, 'item_index'),
        **_draft_changes(arguments))
    return service.format_draft(plan)


def _edit_draft_items(service, arguments, context):
    rows = arguments.get('updates')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError('updates 必须包含 1 到 20 项修改')
    updates = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('updates 中的每一项必须是对象')
        name = _text(row, 'attraction_name')
        updates.append({'attraction_name': name, **_draft_changes(row, subject=name)})
    plan = service.update_draft_items(
        platform=context.platform, group_id=context.group_id, creator_id=context.creator_id,
        plan_code=_text(arguments, 'plan_code'), updates=updates)
    return service.format_draft(plan)


def _add_item(service, arguments, context, operation_key):
    platform = context.platform
    group_id = context.group_id
    creator_id = context.creator_id
    requires = arguments.get('requires_reservation')
    if not isinstance(requires, bool):
        raise ValueError('requires_reservation 必须是布尔值')
    unit = _text(arguments, 'advance_unit')
    if unit not in {'day', 'month', 'none'}:
        raise ValueError('advance_unit 必须是 day、month 或 none')
    try:
        advance_value = int(arguments.get('advance_value', 0))
    except (TypeError, ValueError) as exc:
        raise ValueError('advance_value 必须是整数') from exc
    if not 0 <= advance_value <= 365:
        raise ValueError('advance_value 必须在 0 到 365 之间')
    if requires and (unit == 'none' or advance_value < 1):
        raise ValueError('需要预约时必须提供有效提前天数或月数')
    if not requires:
        unit = 'none'
        advance_value = 0
    plan = service.add_manual_item(
        platform=platform, group_id=group_id, creator_id=creator_id,
        plan_code=_text(arguments, 'plan_code'), attraction_name=_text(arguments, 'attraction_name'),
        visit_date=date.fromisoformat(_text(arguments, 'visit_date')), advance_value=advance_value,
        advance_unit=unit, requires_reservation=requires, operation_key=operation_key)
    return service.format_draft(plan)


def _confirm_plan(service, arguments, context):
    plan = service.confirm_plan(context.platform, context.group_id, context.creator_id, _text(arguments, 'plan_code'))
    return f'预约计划 {plan.plan_code} 已确认。'


def _cancel_plan(service, arguments, context):
    warning = service.cancel_plan(context.platform, context.group_id, context.creator_id, _text(arguments, 'plan_code'))
    return '预约计划已取消。' + (' 提醒正在发送，可能已经发出。' if warning else '')


def _dispatch_reservation(service, create_draft, name, arguments, context, operation_key):
    platform, group_id, creator_id = context.platform, context.group_id, context.creator_id
    handlers = {
        'create_reservation_draft_from_image': lambda: create_draft(
            platform=platform, group_id=group_id, creator_id=creator_id, event_id=context.event_id,
            attachments=context.attachments, attachment_index=_positive_int(arguments, 'attachment_index')),
        'list_reservation_plans': lambda: service.format_plan_list(
            service.list_plans(platform, group_id, creator_id)),
        'refresh_reservation_plan': lambda: _refresh_plan(service, arguments, context),
        'confirm_reservation_plan': lambda: _confirm_plan(service, arguments, context),
        'cancel_reservation_plan': lambda: _cancel_plan(service, arguments, context),
        'complete_reservation_item_date': lambda: service.format_draft(service.complete_item_date(
            platform, group_id, creator_id, _text(arguments, 'plan_code'),
            _positive_int(arguments, 'item_index'), date.fromisoformat(_text(arguments, 'visit_date')))),
        'update_reservation_draft_item': lambda: _edit_draft_item(service, arguments, context),
        'update_reservation_draft_items': lambda: _edit_draft_items(service, arguments, context),
        'add_reservation_item': lambda: _add_item(service, arguments, context, operation_key),
        'set_reservation_reminder_times': lambda: service.format_draft(service.set_draft_reminder_times(
            platform, group_id, creator_id, _text(arguments, 'plan_code'),
            _positive_int(arguments, 'item_index'), _times(arguments))),
        'modify_reservation_item_date': lambda: service.format_mutation(service.modify_item_date(
            platform, group_id, creator_id, _text(arguments, 'item_code'),
            date.fromisoformat(_text(arguments, 'visit_date')))),
        'modify_reservation_item_times': lambda: service.format_mutation(service.modify_item_times(
            platform, group_id, creator_id, _text(arguments, 'item_code'), _times(arguments))),
        'cancel_reservation_item': lambda: service.format_mutation(service.cancel_item(
            platform, group_id, creator_id, _text(arguments, 'item_code'))),
    }
    return handlers[name]()


def reservation_result(service, create_draft, name, arguments, context):
    if context is None:
        return ToolResult('failed', name, data='预约工具缺少用户上下文。', error_code='missing_context')
    resource_id = str(arguments.get('plan_code') or arguments.get('item_code') or '')
    try:
        text = execute_reservation(service, create_draft, name, arguments, context)
    except ReservationConfirmationRequired as exc:
        return ToolResult('needs_input', name, resource_id, data=str(exc), error_code='review_required')
    except (ValueError, PermissionError) as exc:
        return ToolResult('failed', name, resource_id, data=str(exc),
                          error_code='permission_denied' if isinstance(exc, PermissionError) else 'invalid_state_or_arguments')
    return ToolResult('completed', name, resource_id, data=text,
                      effect_status='none' if name == 'list_reservation_plans' else 'committed',
                      operation_key=event_operation_key(context.event_id, name, arguments))


MAX_VISION_RESPONSE_CHARS = 100_000
MAX_VISION_RAW_TEXT_CHARS = 20_000
MAX_VISION_ITEMS = 50
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisionExtraction:
    raw_text: str
    items: tuple[ReservationExtractionItem, ...]

    def as_json_object(self) -> dict[str, object]:
        return {
            "raw_text": self.raw_text,
            "items": [
                {
                    "attraction_name": item.attraction_name,
                    "price_text": item.price_text,
                    "opening_hours": item.opening_hours,
                    "requires_reservation": item.requires_reservation,
                    "advance_value": item.advance_value,
                    "advance_unit": item.advance_unit,
                    "booking_channel": item.booking_channel,
                    "source_text": item.source_text,
                    "confidence": item.confidence,
                }
                for item in self.items
            ],
        }


@dataclass(frozen=True)
class ImageProcessingResult:
    image: ReservationImageRecord
    extraction: VisionExtraction | None


class ImageVisionExtractor:
    def __init__(
            self,
            model_id: str,
            client: object = None,
            api_key: str = "",
            base_url: str = ""):
        self.model_id = model_id
        self.client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=90,
            max_retries=0,
        )

    @skill_request
    def extract(
            self,
            image_bytes: bytes,
            content_type: str) -> VisionExtraction:
        data_url = (
            f"data:{content_type};base64,"
            + base64.b64encode(image_bytes).decode("ascii")
        )
        messages = [
            {
                "role": "system",
                "content": (
                    skill_text('reservation.image', 'system')
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": skill_text('reservation.image', 'image_instruction'),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            },
        ]
        first = self._request(messages)
        try:
            return self._parse(first)
        except (ValueError, json.JSONDecodeError):
            repaired = self._request([
                messages[0],
                {
                    "role": "user",
                    "content": (
                        (skill_text('reservation.image', 'feedback') + first)
                    ),
                },
            ], repair_attempt=1)
            return self._parse(repaired)

    @skill_request
    def _request(self, messages: list[dict[str, object]], repair_attempt: int = 0) -> str:
        with model_call_context('image_extract', prompt_version='reservation-image-v2',
                                repair_attempt=repair_attempt, stage_id='reservation.image'):
            response = self.client.chat.completions.create(
                model=self.model_id,
                messages=messages,
            )
        return str(response_text(response)).strip()

    @staticmethod
    def _parse(raw: str) -> VisionExtraction:
        if len(raw) > MAX_VISION_RESPONSE_CHARS:
            raise ValueError("vision response is too large")
        cleaned = raw.strip()
        fence = chr(96) * 3
        if cleaned.startswith(fence):
            lines = cleaned.splitlines()
            cleaned = "\n".join(lines[1:-1]).strip()
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise ValueError("vision response must be a JSON object")
        raw_text = str(payload.get("raw_text") or "").strip()
        if len(raw_text) > MAX_VISION_RAW_TEXT_CHARS:
            raise ValueError("vision raw_text is too large")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("vision response items must be a JSON array")
        if len(raw_items) > MAX_VISION_ITEMS:
            raise ValueError("vision response contains too many items")
        items = tuple(
            normalize_extraction_item(item)
            for item in raw_items
            if isinstance(item, dict)
        )
        if len(items) != len(raw_items):
            raise ValueError("every vision item must be a JSON object")
        return VisionExtraction(raw_text=raw_text, items=items)


class ReservationImageService:
    def __init__(
            self,
            store: MemoryStore,
            extractor: ImageVisionExtractor | None,
            image_root: str | Path | None = None,
            session: object = None,
            resolver=resolve_host):
        self.store = store
        self.extractor = extractor
        self.image_root = Path(image_root or default_image_root())
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.resolver = resolver

    is_supported_attachment = staticmethod(is_supported_attachment)

    def process_attachment(
            self,
            storage_scope_id: str,
            platform: str,
            group_id: str,
            uploader_id: str,
            attachment: object) -> ImageProcessingResult:
        image_bytes, content_type = self._download(attachment)
        digest = hashlib.sha256(image_bytes).hexdigest()
        extension = CONTENT_TYPE_EXTENSIONS[content_type]
        destination = self.image_root / digest[:2] / f"{digest}{extension}"
        temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.part")
        with publish_files(self.store, platform, group_id, (destination, temporary)):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                try:
                    with temporary.open("wb") as handle:
                        handle.write(image_bytes)
                    os.replace(temporary, destination)
                finally:
                    if temporary.exists():
                        temporary.unlink()

            image, is_new = self.store.reservations.create_reservation_image(
                storage_scope_id=storage_scope_id,
                platform=platform,
                group_id=group_id,
                uploader_id=uploader_id,
                sha256=digest,
                file_path=str(destination),
                content_type=content_type,
                byte_size=len(image_bytes),
                model_id=(self.extractor.model_id if self.extractor else ""),
            )
        if not is_new:
            if image.status == "extracted":
                extraction = ImageVisionExtractor._parse(
                    json.dumps(image.extraction, ensure_ascii=False)
                )
                return ImageProcessingResult(image, extraction)
            if image.status == "pending":
                return ImageProcessingResult(image, None)
            if not self.store.reservations.restart_failed_reservation_image(image.image_id):
                refreshed = self.store.reservations.get_reservation_image(
                    storage_scope_id,
                    digest,
                )
                return ImageProcessingResult(refreshed, None)

        if self.extractor is None:
            self.store.reservations.mark_reservation_image_failed(
                image.image_id,
                "multimodal model is not configured",
            )
            failed = self.store.reservations.get_reservation_image(
                storage_scope_id,
                digest,
            )
            return ImageProcessingResult(failed, None)

        try:
            extraction = self.extractor.extract(image_bytes, content_type)
            self.store.reservations.mark_reservation_image_extracted(
                image.image_id,
                extraction.raw_text,
                extraction.as_json_object(),
                self.extractor.model_id,
            )
        except Exception as exc:
            self.store.reservations.mark_reservation_image_failed(
                image.image_id,
                type(exc).__name__,
            )
            failed = self.store.reservations.get_reservation_image(
                storage_scope_id,
                digest,
            )
            logger.warning(
                "Reservation image extraction failed: "
                "sha=%s bytes=%s model=%s",
                digest[:12],
                len(image_bytes),
                self.extractor.model_id,
            )
            return ImageProcessingResult(failed, None)

        completed = self.store.reservations.get_reservation_image(
            storage_scope_id,
            digest,
        )
        logger.info(
            "Reservation image extracted: sha=%s bytes=%s model=%s items=%s",
            digest[:12],
            len(image_bytes),
            self.extractor.model_id,
            len(extraction.items),
        )
        return ImageProcessingResult(completed, extraction)

    def _download(self, attachment):
        return download_image(attachment, session=self.session,
                              database_path=self.store.database_path, resolver=self.resolver)


RESERVATION_WORKFLOW_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class ReservationRefreshResult:
    plan: object
    updated_count: int


class ReservationConfirmationRequired(ValueError):
    """A newly resolved or conflicting draft must be shown before confirmation."""


class ReservationTool:
    def __init__(
            self,
            store: object,
            itinerary_resolver: ItineraryResolver | None = None, *, image_service=None):
        self.store = store
        self.image_service = image_service
        self.itinerary_resolver = (
            itinerary_resolver or ReservationItineraryResolver()
        )

    def create_from_attachment(self, *, platform, group_id, creator_id, event_id, attachments, attachment_index):
        if self.image_service is None:
            raise ValueError('预约图片服务未配置。')
        return create_reservation_from_attachment(self, self.image_service, platform=platform,
            group_id=group_id, creator_id=creator_id, event_id=event_id,
            attachments=attachments, attachment_index=attachment_index)

    create = create_from_attachment

    def execute(self, name, arguments, context):
        return execute_reservation(self, self.create_from_attachment, name, arguments, context)

    def execute_result(self, name, arguments, context=None):
        return reservation_result(self, self.create_from_attachment, name, arguments, context)

    def start_workflow(
            self,
            platform: str,
            group_id: str,
            creator_id: str) -> None:
        self.store.reservations.start_reservation_workflow(
            platform,
            group_id,
            creator_id,
            duration=RESERVATION_WORKFLOW_TTL,
        )

    def workflow_is_active(
            self,
            platform: str,
            group_id: str,
            creator_id: str) -> bool:
        return self.store.reservations.reservation_workflow_is_active(
            platform,
            group_id,
            creator_id,
        )

    def finish_workflow(
            self,
            platform: str,
            group_id: str,
            creator_id: str) -> bool:
        return self.store.reservations.clear_reservation_workflow(
            platform,
            group_id,
            creator_id,
        )

    def create_draft(
            self,
            image: object,
            extraction_items: Sequence[ReservationExtractionItem],
            now: datetime | None = None,
            source_event_id: str = "",
            creator_id: str | None = None):
        required_names = tuple(dict.fromkeys(
            extraction.attraction_name
            for extraction in extraction_items
            if extraction.requires_reservation
        ))
        resolutions = {}
        if required_names:
            documents = self.store.documents.list_document_contents(
                image.storage_scope_id
            )
            resolutions = self.itinerary_resolver.resolve(
                documents,
                required_names,
            )

        draft_items = []
        for extraction in extraction_items:
            if not extraction.requires_reservation or extraction.advance_unit == "none":
                draft_items.append({
                    "extraction": extraction,
                    "visit_date": None,
                    "booking_date": None,
                    "date_candidates": (),
                    "custom_reminder_times": (),
                    "reminder_policy": "none",
                    "status": "needs_input" if extraction.requires_reservation else "ready",
                })
                continue

            resolution = resolutions.get(
                extraction.attraction_name,
                VisitDateResolution((), "not_found"),
            )
            candidates = resolution.dates
            visit_date = candidates[0] if len(candidates) == 1 else None
            booking_date = (
                calculate_booking_date(
                    visit_date,
                    extraction.advance_value,
                    extraction.advance_unit,
                )
                if visit_date is not None
                else None
            )
            status = "ready" if visit_date is not None else "needs_input"
            if resolution.reason == "not_scheduled":
                status = "not_scheduled"
            draft_items.append({
                "extraction": extraction,
                "visit_date": visit_date,
                "booking_date": booking_date,
                "date_candidates": candidates,
                "date_source": resolution.source,
                "custom_reminder_times": (),
                "reminder_policy": "default",
                "status": status,
            })

        return self.store.reservations.create_reservation_draft(
            image_id=image.image_id,
            platform=image.platform,
            group_id=image.group_id,
            creator_id=creator_id if creator_id is not None else image.uploader_id,
            items=tuple(draft_items),
            now=now,
            source_event_id=source_event_id,
        )

    def refresh_plan(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str) -> ReservationRefreshResult:
        plan = self.store.reservations.get_reservation_plan(platform, group_id, plan_code)
        if plan is None:
            raise ValueError("预约计划不存在")
        if plan.creator_id != creator_id:
            raise PermissionError("只有创建者可以刷新预约计划")
        if plan.status != "draft":
            return ReservationRefreshResult(plan, 0)

        refresh_revision = self.store.reservations.claim_reservation_refresh(
            platform,
            group_id,
            creator_id,
            plan_code,
        )
        plan = self.store.reservations.get_reservation_plan(platform, group_id, plan_code)
        if plan is None:
            raise RuntimeError("reservation draft disappeared during refresh")
        if refresh_revision is None or plan.status != "draft":
            return ReservationRefreshResult(plan, 0)

        items = tuple(
            item
            for item in plan.items
            if item.requires_reservation
            and item.advance_unit != "none"
            and item.visit_date is None
            and item.status in {"needs_input", "not_scheduled"}
        )
        documents = self.store.documents.list_document_contents(
            storage_scope_id(platform, group_id)
        )
        resolutions = self.itinerary_resolver.resolve(
            documents,
            tuple(item.attraction_name for item in plan.items if item.requires_reservation),
        )
        conflicts = []
        for existing in plan.items:
            resolved = resolutions.get(existing.attraction_name)
            if (existing.visit_date and resolved and resolved.dates
                    and existing.date_source.get("origin") != "manual"
                    and existing.visit_date not in resolved.dates):
                conflicts.append(
                    f"{existing.item_index}. {existing.attraction_name}：保留 {existing.visit_date}；"
                    f"文档 {resolved.source.get('filename', '行程')} 建议 "
                    + "、".join(str(value) for value in resolved.dates)
                )
        if conflicts:
            return ReservationRefreshResult(replace(plan, source_conflicts=tuple(conflicts)), 0)
        if not items:
            return ReservationRefreshResult(plan, 0)
        updates = []
        for item in items:
            resolution = resolutions.get(
                item.attraction_name,
                VisitDateResolution((), "not_found"),
            )
            visit_date = (
                resolution.dates[0]
                if len(resolution.dates) == 1
                else None
            )
            booking_date = (
                calculate_booking_date(
                    visit_date,
                    item.advance_value,
                    item.advance_unit,
                )
                if visit_date is not None
                else None
            )
            status = "ready" if visit_date is not None else "needs_input"
            if resolution.reason == "not_scheduled":
                status = "not_scheduled"
            updates.append({
                "item_index": item.item_index,
                "visit_date": visit_date,
                "booking_date": booking_date,
                "date_candidates": resolution.dates,
                "date_source": resolution.source,
                "status": status,
                "refresh_revision": refresh_revision,
            })

        changed = self.store.reservations.refresh_reservation_draft_items(
            platform,
            group_id,
            creator_id,
            plan_code,
            tuple(updates),
        )
        refreshed = self.store.reservations.get_reservation_plan(
            platform, group_id, plan_code
        )
        if refreshed is None:
            raise RuntimeError("reservation draft disappeared during refresh")
        return ReservationRefreshResult(refreshed, changed)

    def format_draft(self, plan: object) -> str:
        lines = [f"预约计划 {plan.plan_code}（版本 {plan.plan_version}）", ""]
        if plan.source_conflicts:
            lines.extend(("行程来源冲突，尚未自动合并：", *plan.source_conflicts,
                          "请用补充预约命令逐项确定日期（保留原日期也请明确填写）。"))
        if not plan.items:
            lines.extend([
                "图片已保存，但未提取到景点。",
                (
                    f"请使用：新增预约 {plan.plan_code} "
                    "景点名称 YYYY-MM-DD 提前N天"
                ),
            ])
            return "\n".join(lines)

        for item in plan.items:
            lines.append(f"{item.item_index}. {item.attraction_name}")
            if item.confidence < 0.85:
                lines.append("   识别置信度较低，请人工核对")
            if item.status == "skipped":
                lines.append("   状态：不去参观")
                continue
            if not item.requires_reservation:
                lines.append("   无需预约，仅保存信息")
                if item.visit_date:
                    lines.append(f"   游览日期：{item.visit_date.isoformat()}")
                continue
            if item.advance_unit == "none":
                lines.append("   需要预约，但无可用提前规则；不自动生成开约提醒，请人工核对并补充规则")
                continue
            if item.status == "not_scheduled":
                lines.append("   游览日期：未确定")
                lines.append("   状态：行程未安排该景点，需要手动决定")
                continue
            lines.append(
                "   游览日期："
                + (item.visit_date.isoformat() if item.visit_date else "未确定")
            )
            if item.visit_date:
                source = item.date_source
                label = (f"文档 #{source.get('document_id')} {source.get('filename')}"
                         if source.get("origin") == "document"
                         else "人工指定" if source.get("origin") == "manual" else "历史记录/来源未知")
                lines.append(f"   日期来源：{label}")
            if item.booking_date:
                lines.append(
                    f"   建议预约日期：{item.booking_date.isoformat()}"
                )
                occurrences = build_reminder_occurrences(
                    item.booking_date,
                    item.custom_reminder_times,
                )
                displayed = "、".join(
                    value.scheduled_at_utc.astimezone(
                        BEIJING_TZ
                    ).strftime(ABSOLUTE_TIME_FORMAT)
                    for value in occurrences
                )
                lines.append(f"   提醒：{displayed}")
            elif item.date_candidates:
                lines.append(
                    "   候选日期："
                    + "、".join(
                        value.isoformat()
                        for value in item.date_candidates
                    )
                )
            else:
                lines.append("   状态：需要补充日期")
        lines.extend([
            "",
            f"确认前可补充或修改；确认命令：确认预约 {plan.plan_code}",
        ])
        return "\n".join(lines)

    def complete_item_date(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            item_index: int,
            visit_date: date):
        plan = self.store.reservations.get_reservation_plan(platform, group_id, plan_code)
        if plan is None or plan.creator_id != creator_id:
            raise ValueError("未找到可修改的预约计划")
        item = next(
            (
                value
                for value in plan.items
                if value.item_index == item_index
            ),
            None,
        )
        if item is None or not item.requires_reservation:
            raise ValueError("该项目不需要补充预约日期")
        booking_date = calculate_booking_date(
            visit_date,
            item.advance_value,
            item.advance_unit,
        )
        changed = self.store.reservations.update_reservation_draft_item_date(
            platform,
            group_id,
            creator_id,
            plan_code,
            item_index,
            visit_date,
            booking_date,
        )
        if not changed:
            raise ValueError("预约计划当前无法修改")
        return self.store.reservations.get_reservation_plan(platform, group_id, plan_code)

    def update_draft_item(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            item_index: int,
            visit_date: date | None = None,
            requires_reservation: bool | None = None,
            visit_status: Literal["visit", "skip"] | None = None):
        plan = self.store.reservations.get_reservation_plan(platform, group_id, plan_code)
        if plan is None or plan.creator_id != creator_id:
            raise ValueError("未找到可修改的预约计划")
        if plan.status != "draft":
            raise ValueError("预约计划当前无法修改")
        item = next(
            (
                value
                for value in plan.items
                if value.item_index == item_index
            ),
            None,
        )
        if item is None:
            raise ValueError("未找到预约项目")
        values = self._draft_item_update_values(
            item,
            visit_date,
            requires_reservation,
            visit_status,
        )

        changed = self.store.reservations.update_reservation_draft_item(
            platform=platform,
            group_id=group_id,
            creator_id=creator_id,
            plan_code=plan_code,
            item_index=item_index,
            **values,
        )
        if not changed:
            raise ValueError("预约计划当前无法修改")
        return self.store.reservations.get_reservation_plan(platform, group_id, plan_code)

    def update_draft_items(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            updates: Sequence[Mapping[str, object]]):
        plan = self.store.reservations.get_reservation_plan(platform, group_id, plan_code)
        if plan is None or plan.creator_id != creator_id:
            raise ValueError("未找到可修改的预约计划")
        if plan.status != "draft":
            raise ValueError("预约计划当前无法修改")
        if not updates:
            raise ValueError("至少需要提供一项草稿修改")

        items_by_name = {}
        for item in plan.items:
            name = item.attraction_name.strip()
            if name in items_by_name:
                raise ValueError(f"草稿中存在重名景点：{name}")
            items_by_name[name] = item

        prepared = []
        seen_names = set()
        for update in updates:
            attraction_name = str(update.get("attraction_name") or "").strip()
            if not attraction_name:
                raise ValueError("缺少景点名称")
            if attraction_name in seen_names:
                raise ValueError(f"景点重复修改：{attraction_name}")
            seen_names.add(attraction_name)
            item = items_by_name.get(attraction_name)
            if item is None:
                raise ValueError(f"草稿中未找到景点：{attraction_name}")
            values = self._draft_item_update_values(
                item,
                update.get("visit_date"),
                update.get("requires_reservation"),
                update.get("visit_status"),
            )
            prepared.append({"item_index": item.item_index, **values})

        changed = self.store.reservations.update_reservation_draft_items(
            platform=platform,
            group_id=group_id,
            creator_id=creator_id,
            plan_code=plan_code,
            updates=prepared,
        )
        if not changed:
            raise ValueError("预约计划当前无法修改")
        return self.store.reservations.get_reservation_plan(platform, group_id, plan_code)

    @staticmethod
    def _draft_item_update_values(
            item,
            visit_date: date | None,
            requires_reservation: bool | None,
            visit_status: Literal["visit", "skip"] | None
    ) -> dict[str, object]:
        if visit_status not in {None, "visit", "skip"}:
            raise ValueError("visit_status 必须是 visit 或 skip")
        if requires_reservation is not None and not isinstance(
                requires_reservation,
                bool):
            raise ValueError("requires_reservation 必须是布尔值")
        if visit_date is not None and not isinstance(visit_date, date):
            raise ValueError("visit_date 必须是日期")

        if visit_status == "skip":
            return {
                "requires_reservation": False,
                "visit_date": None,
                "booking_date": None,
                "date_candidates": (),
                "custom_reminder_times": (),
                "reminder_policy": "none",
                "status": "skipped",
            }

        target_requires = (
            item.requires_reservation
            if requires_reservation is None
            else requires_reservation
        )
        target_visit_date = visit_date or item.visit_date
        date_candidates = (
            (target_visit_date,)
            if target_visit_date
            else item.date_candidates
        )
        if target_requires:
            if item.advance_unit == "none" or item.advance_value < 1:
                raise ValueError("该项目缺少有效的提前预约规则")
            booking_date = (
                calculate_booking_date(
                    target_visit_date,
                    item.advance_value,
                    item.advance_unit,
                )
                if target_visit_date
                else None
            )
            custom_times = item.custom_reminder_times
            reminder_policy = (
                item.reminder_policy
                if item.reminder_policy != "none"
                else "default"
            )
            status = "ready" if target_visit_date else "needs_input"
        else:
            booking_date = None
            custom_times = ()
            reminder_policy = "none"
            status = "ready"
        return {
            "requires_reservation": target_requires,
            "visit_date": target_visit_date,
            "booking_date": booking_date,
            "date_candidates": date_candidates,
            "custom_reminder_times": custom_times,
            "reminder_policy": reminder_policy,
            "status": status,
        }

    def add_manual_item(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            attraction_name: str,
            visit_date: date,
            advance_value: int,
            advance_unit: AdvanceUnit,
            requires_reservation: bool,
            operation_key: str = ""):
        extraction = ReservationExtractionItem(
            attraction_name=attraction_name.strip(),
            price_text="",
            opening_hours="",
            requires_reservation=requires_reservation,
            advance_value=advance_value if requires_reservation else 0,
            advance_unit=advance_unit if requires_reservation else "none",
            booking_channel="",
            source_text="用户手动新增",
            confidence=1.0,
        )
        if requires_reservation and advance_unit == "none":
            raise ValueError("需要预约但无可用提前规则，不能创建开约提醒")
        booking_date = (
            calculate_booking_date(
                visit_date,
                extraction.advance_value,
                extraction.advance_unit,
            )
            if requires_reservation
            else None
        )
        changed = self.store.reservations.append_reservation_draft_item(
            platform,
            group_id,
            creator_id,
            plan_code,
            extraction,
            visit_date,
            booking_date,
            "default" if requires_reservation else "none",
            "ready",
            source_operation_key=operation_key,
        )
        if not changed:
            raise ValueError("未找到可修改的预约计划")
        return self.store.reservations.get_reservation_plan(platform, group_id, plan_code)

    def set_draft_reminder_times(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str,
            item_index: int,
            custom_times: Sequence[datetime]):
        if not custom_times:
            raise ValueError("至少需要一个完整提醒时间")
        changed = self.store.reservations.set_reservation_draft_item_times(
            platform,
            group_id,
            creator_id,
            plan_code,
            item_index,
            tuple(custom_times),
        )
        if not changed:
            raise ValueError("未找到可设置提醒的预约项目")
        return self.store.reservations.get_reservation_plan(platform, group_id, plan_code)

    def confirm_plan(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str):
        refresh = self.refresh_plan(platform, group_id, creator_id, plan_code)
        plan = refresh.plan
        if plan.source_conflicts:
            raise ReservationConfirmationRequired(self.format_draft(plan))
        if refresh.updated_count:
            raise ReservationConfirmationRequired("确认前已补齐新日期，请核对以下草稿后再次确认。\n" + self.format_draft(plan))
        reminders = {}
        for item in plan.items:
            if item.requires_reservation:
                if item.advance_unit == "none":
                    raise ValueError("需要预约但无可用提前规则，请先人工核对并补充；尚未创建开约提醒")
                if item.status == "needs_input" or item.booking_date is None:
                    raise ValueError("请先补充所有需要预约项目的日期")
                reminders[item.item_id] = build_reminder_occurrences(
                    item.booking_date,
                    item.custom_reminder_times,
                )
        return self.store.reservations.confirm_reservation_plan(
            platform,
            group_id,
            creator_id,
            plan_code,
            reminders,
            expected_version=plan.plan_version,
        )

    def list_plans(
            self,
            platform: str,
            group_id: str,
            creator_id: str):
        plans = self.store.reservations.list_reservation_plans_for_creator(
            platform,
            group_id,
            creator_id,
        )
        if (
                not plans
                and self.store.reservations.group_has_reservation_plans(
                    platform,
                    group_id,
                )):
            raise PermissionError("只有创建者可以查看预约提醒")
        refreshed = []
        for plan in plans:
            if plan.status == "draft":
                plan = self.refresh_plan(
                    platform,
                    group_id,
                    creator_id,
                    plan.plan_code,
                ).plan
            refreshed.append(plan)
        return tuple(refreshed)

    def format_plan_list(self, plans: Sequence[object]) -> str:
        if not plans:
            return "当前没有预约提醒"
        lines = []
        for plan in plans:
            lines.append(f"{plan.plan_code}（{plan.status}，版本 {plan.plan_version}）")
            if plan.source_conflicts:
                lines.extend(("行程来源冲突，请逐项补充日期后确认：", *plan.source_conflicts))
            for item in plan.items:
                visit = (
                    item.visit_date.isoformat()
                    if item.visit_date
                    else "未定"
                )
                booking = (
                    item.booking_date.isoformat()
                    if item.booking_date
                    else "未提供可用提前规则" if item.requires_reservation else "无需预约"
                )
                reminder_text = "无"
                if item.booking_date and item.status == "confirmed":
                    reminder_text = "、".join(
                        occurrence.scheduled_at_utc.astimezone(
                            BEIJING_TZ
                        ).strftime(ABSOLUTE_TIME_FORMAT)
                        for occurrence in build_reminder_occurrences(
                            item.booking_date,
                            item.custom_reminder_times,
                        )
                    )
                lines.append(
                    f"- {item.public_code} {item.attraction_name} "
                    f"游览 {visit}，预约 {booking}，"
                    f"提醒 {reminder_text}，状态 {item.status}"
                )
        return "\n".join(lines)

    def modify_item_date(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            public_code: str,
            visit_date: date):
        item = self.store.reservations.get_reservation_item(
            platform,
            group_id,
            creator_id,
            public_code,
        )
        if item is None:
            raise PermissionError("只有创建者可以修改预约提醒")
        if item.requires_reservation and item.advance_unit == "none":
            raise ValueError("需要预约但无可用提前规则，不能创建开约提醒")
        booking_date = calculate_booking_date(
            visit_date,
            item.advance_value,
            item.advance_unit,
        )
        occurrences = build_reminder_occurrences(
            booking_date,
            item.custom_reminder_times,
        )
        return self.store.reservations.replace_reservation_item_schedule(
            platform=platform,
            group_id=group_id,
            creator_id=creator_id,
            public_code=public_code,
            visit_date=visit_date,
            booking_date=booking_date,
            custom_times=item.custom_reminder_times,
            reminder_policy=item.reminder_policy,
            occurrences=occurrences,
        )

    def modify_item_times(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            public_code: str,
            custom_times: Sequence[datetime]):
        item = self.store.reservations.get_reservation_item(
            platform,
            group_id,
            creator_id,
            public_code,
        )
        if item is None:
            raise PermissionError("只有创建者可以修改预约提醒")
        if not custom_times:
            raise ValueError("至少需要一个完整提醒时间")
        if item.booking_date is None or item.visit_date is None:
            raise ValueError("预约项目缺少日期")
        occurrences = build_reminder_occurrences(
            item.booking_date,
            custom_times,
        )
        return self.store.reservations.replace_reservation_item_schedule(
            platform=platform,
            group_id=group_id,
            creator_id=creator_id,
            public_code=public_code,
            visit_date=item.visit_date,
            booking_date=item.booking_date,
            custom_times=tuple(custom_times),
            reminder_policy="custom",
            occurrences=occurrences,
        )

    def cancel_item(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            public_code: str):
        result = self.store.reservations.cancel_reservation_item(
            platform,
            group_id,
            creator_id,
            public_code,
        )
        if result is None:
            raise PermissionError("只有创建者可以取消预约提醒")
        return result

    def cancel_plan(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str) -> bool:
        result = self.store.reservations.cancel_reservation_plan(
            platform,
            group_id,
            creator_id,
            plan_code,
        )
        if result is None:
            raise PermissionError("只有创建者可以取消预约计划")
        return result

    @staticmethod
    def format_mutation(result: object) -> str:
        message = (
            f"{result.item.public_code} {result.item.attraction_name} "
            f"已更新，当前状态 {result.item.status}。"
        )
        if result.sending_warning:
            message += " 提醒正在发送，可能已经发出。"
        return message

    def handle_command(self, command: object, event: object) -> str:
        event_id = str(getattr(event, "event_key", "") or "")
        if not event_id:
            return self._handle_command_uncached(command, event)
        arguments = {
            "args": list(command.args),
        }
        operation_key = event_operation_key(
            event_id,
            command.name,
            arguments,
        )
        cached = self.store.delivery.get_event_tool_result(operation_key)
        if cached is not None:
            return cached
        result = self._handle_command_uncached(
            command,
            event,
            operation_key=operation_key,
        )
        return self.store.delivery.save_event_tool_result(
            operation_key,
            event_id,
            command.name,
            arguments,
            result,
        )

    def _handle_command_uncached(
            self,
            command: object,
            event: object,
            operation_key: str = "") -> str:
        name = command.name
        args = command.args
        platform = event.platform
        group_id = event.scope_id
        creator_id = event.sender_id

        if name == "reservation_start":
            self.start_workflow(platform, group_id, creator_id)
            return (
                "已进入预约制定模式。请在 30 分钟内发送一张预约攻略图片，"
                "机器人会识别景点是否需要预约及提前时间。"
                "发送“退出制定预约”可以取消。"
            )
        if name == "reservation_stop":
            if self.finish_workflow(platform, group_id, creator_id):
                return "已退出预约制定模式。"
            return "当前没有正在进行的预约制定流程。"
        if name == "reservation_list":
            return self.format_plan_list(
                self.list_plans(platform, group_id, creator_id)
            )
        if name == "reservation_complete_date":
            plan = self.complete_item_date(
                platform,
                group_id,
                creator_id,
                args[0],
                int(args[1]),
                date.fromisoformat(args[2]),
            )
            return self.format_draft(plan)
        if name == "reservation_add_item":
            plan = self.add_manual_item(
                platform=platform,
                group_id=group_id,
                creator_id=creator_id,
                plan_code=args[0],
                attraction_name=args[1],
                visit_date=date.fromisoformat(args[2]),
                advance_value=int(args[3]),
                advance_unit=args[4],
                requires_reservation=args[5] == "1",
                operation_key=operation_key,
            )
            return self.format_draft(plan)
        if name == "reservation_set_times":
            plan = self.set_draft_reminder_times(
                platform,
                group_id,
                creator_id,
                args[0],
                int(args[1]),
                parse_beijing_datetime_list(args[2]),
            )
            return self.format_draft(plan)
        if name == "reservation_refresh":
            result = self.refresh_plan(
                platform,
                group_id,
                creator_id,
                args[0],
            )
            if result.plan.status != "draft":
                raise ValueError("只有未确认的预约草稿可以刷新")
            if result.updated_count:
                prefix = f"已按最新行程刷新 {result.updated_count} 个项目。\n"
            else:
                prefix = "未找到可自动补齐的新日期。\n"
            return prefix + self.format_draft(result.plan)
        if name == "reservation_confirm_help":
            return (
                "请先发送“查看预约提醒”获取计划编号，"
                "再发送“确认预约 R-YYYYMMDD-NNN”。"
            )
        if name == "reservation_confirm":
            plan = self.confirm_plan(
                platform,
                group_id,
                creator_id,
                args[0],
            )
            return f"预约计划 {plan.plan_code} 已确认。"
        if name == "reservation_cancel_plan":
            warning = self.cancel_plan(
                platform,
                group_id,
                creator_id,
                args[0],
            )
            return (
                "预约计划已取消。"
                + (" 提醒正在发送，可能已经发出。" if warning else "")
            )
        if name == "reservation_modify_date":
            result = self.modify_item_date(
                platform,
                group_id,
                creator_id,
                args[0],
                date.fromisoformat(args[1]),
            )
            return self.format_mutation(result)
        if name == "reservation_modify_times":
            result = self.modify_item_times(
                platform,
                group_id,
                creator_id,
                args[0],
                parse_beijing_datetime_list(args[1]),
            )
            return self.format_mutation(result)
        if name == "reservation_cancel_item":
            result = self.cancel_item(
                platform,
                group_id,
                creator_id,
                args[0],
            )
            return self.format_mutation(result)
        raise ValueError("不是预约提醒命令")
