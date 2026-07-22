from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo


AdvanceUnit = Literal["day", "month", "none"]
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
ABSOLUTE_TIME_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True)
class ReservationExtractionItem:
    attraction_name: str
    price_text: str
    opening_hours: str
    requires_reservation: bool
    advance_value: int
    advance_unit: AdvanceUnit
    booking_channel: str
    source_text: str
    confidence: float


@dataclass(frozen=True)
class ReminderOccurrence:
    scheduled_at_utc: datetime
    is_custom: bool


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def normalize_extraction_item(
        payload: Mapping[str, object]) -> ReservationExtractionItem:
    attraction_name = _required_text(payload, "attraction_name")
    requires_reservation = bool(payload.get("requires_reservation"))
    advance_unit = str(payload.get("advance_unit") or "").strip().lower()
    if advance_unit not in {"day", "month", "none"}:
        raise ValueError("advance_unit must be day, month, or none")

    raw_value = payload.get("advance_value", 0)
    try:
        advance_value = int(raw_value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("advance_value must be an integer") from exc

    if not requires_reservation or advance_unit == "none":
        requires_reservation = False
        advance_unit = "none"
        advance_value = 0
    elif advance_value < 1:
        raise ValueError("advance_value must be at least 1")

    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be numeric") from exc
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")

    return ReservationExtractionItem(
        attraction_name=attraction_name,
        price_text=str(payload.get("price_text") or "").strip(),
        opening_hours=str(payload.get("opening_hours") or "").strip(),
        requires_reservation=requires_reservation,
        advance_value=advance_value,
        advance_unit=advance_unit,
        booking_channel=str(payload.get("booking_channel") or "").strip(),
        source_text=str(payload.get("source_text") or "").strip(),
        confidence=confidence,
    )


def calculate_booking_date(
        visit_date: date,
        advance_value: int,
        advance_unit: AdvanceUnit) -> date | None:
    if advance_unit == "none":
        return None
    if advance_value < 1:
        raise ValueError("advance_value must be at least 1")
    if advance_unit == "day":
        return visit_date - timedelta(days=advance_value)
    if advance_unit != "month":
        raise ValueError("advance_unit must be day, month, or none")

    absolute_month = (
        visit_date.year * 12
        + visit_date.month
        - 1
        - advance_value
    )
    target_year, month_index = divmod(absolute_month, 12)
    target_month = month_index + 1
    last_day = calendar.monthrange(target_year, target_month)[1]
    return date(
        target_year,
        target_month,
        min(visit_date.day, last_day),
    )


def parse_beijing_datetime_list(value: str) -> tuple[datetime, ...]:
    parts = [item.strip() for item in re.split(r"[,，]", value) if item.strip()]
    if not parts:
        raise ValueError("reminder time must use YYYY-MM-DD HH:MM")
    parsed = []
    for item in parts:
        try:
            local_time = datetime.strptime(item, ABSOLUTE_TIME_FORMAT)
        except ValueError as exc:
            raise ValueError(
                "reminder time must use YYYY-MM-DD HH:MM"
            ) from exc
        parsed.append(local_time.replace(tzinfo=BEIJING_TZ))
    return tuple(sorted(set(parsed)))


def build_reminder_occurrences(
        booking_date: date,
        custom_times: Sequence[datetime]) -> tuple[ReminderOccurrence, ...]:
    if custom_times:
        normalized = []
        for value in custom_times:
            if value.tzinfo is None:
                raise ValueError("custom reminder time must be timezone-aware")
            normalized.append(value.astimezone(timezone.utc))
        return tuple(
            ReminderOccurrence(scheduled_at_utc=value, is_custom=True)
            for value in sorted(set(normalized))
        )

    local_values = (
        datetime.combine(
            booking_date - timedelta(days=1),
            time(hour=20),
            tzinfo=BEIJING_TZ,
        ),
        datetime.combine(
            booking_date,
            time(hour=9),
            tzinfo=BEIJING_TZ,
        ),
    )
    return tuple(
        ReminderOccurrence(
            scheduled_at_utc=value.astimezone(timezone.utc),
            is_custom=False,
        )
        for value in local_values
    )


class VisitDateExtractor(Protocol):
    def extract(
            self,
            attraction_name: str,
            evidence: str) -> tuple[date, ...]:
        raise RuntimeError("protocol method")


class LLMVisitDateExtractor:
    def __init__(self, model_id: str, client: object):
        self.model_id = model_id
        self.client = client

    def extract(
            self,
            attraction_name: str,
            evidence: str) -> tuple[date, ...]:
        response = self.client.chat.completions.create(
            model=self.model_id,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "只从不可信行程片段中提取明确写出的完整公历日期。"
                        "不得推测年份，不得根据预约规则计算日期。"
                        "返回单个 JSON 对象，格式为 "
                        "{\"dates\":[\"YYYY-MM-DD\"]}。"
                        "若没有完整日期则返回空数组。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"景点：{attraction_name}\n"
                        "<untrusted_itinerary>\n"
                        + evidence.replace("<", "＜").replace(">", "＞")
                        + "\n</untrusted_itinerary>"
                    ),
                },
            ],
        )
        raw = str(response.choices[0].message.content or "").strip()
        fence = chr(96) * 3
        if raw.startswith(fence):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1]).strip()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return ()
        raw_dates = payload.get("dates")
        if not isinstance(raw_dates, list):
            return ()
        parsed = []
        for value in raw_dates:
            try:
                parsed.append(date.fromisoformat(str(value)))
            except ValueError:
                return ()
        return tuple(sorted(set(parsed)))


class ReservationService:
    def __init__(
            self,
            store: object,
            date_extractor: VisitDateExtractor | None = None):
        self.store = store
        self.date_extractor = date_extractor

    def create_draft(
            self,
            image: object,
            extraction_items: Sequence[ReservationExtractionItem],
            now: datetime | None = None):
        draft_items = []
        for extraction in extraction_items:
            if not extraction.requires_reservation:
                draft_items.append({
                    "extraction": extraction,
                    "visit_date": None,
                    "booking_date": None,
                    "date_candidates": (),
                    "custom_reminder_times": (),
                    "reminder_policy": "none",
                    "status": "ready",
                })
                continue

            evidence = self.store.build_document_context(
                image.storage_scope_id,
                extraction.attraction_name,
                max_chars=1600,
            )
            candidates = (
                self.date_extractor.extract(
                    extraction.attraction_name,
                    evidence,
                )
                if evidence and self.date_extractor
                else ()
            )
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
            draft_items.append({
                "extraction": extraction,
                "visit_date": visit_date,
                "booking_date": booking_date,
                "date_candidates": candidates,
                "custom_reminder_times": (),
                "reminder_policy": "default",
                "status": "ready" if visit_date is not None else "needs_input",
            })

        return self.store.create_reservation_draft(
            image_id=image.image_id,
            platform=image.platform,
            group_id=image.group_id,
            creator_id=image.uploader_id,
            items=tuple(draft_items),
            now=now,
        )

    def format_draft(self, plan: object) -> str:
        lines = [f"预约计划 {plan.plan_code}", ""]
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
            if not item.requires_reservation:
                lines.append("   无需预约，仅保存信息")
                continue
            lines.append(
                "   游览日期："
                + (item.visit_date.isoformat() if item.visit_date else "未确定")
            )
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
        plan = self.store.get_reservation_plan(platform, group_id, plan_code)
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
        changed = self.store.update_reservation_draft_item_date(
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
        return self.store.get_reservation_plan(platform, group_id, plan_code)

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
            requires_reservation: bool):
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
        booking_date = (
            calculate_booking_date(
                visit_date,
                extraction.advance_value,
                extraction.advance_unit,
            )
            if requires_reservation
            else None
        )
        changed = self.store.append_reservation_draft_item(
            platform,
            group_id,
            creator_id,
            plan_code,
            extraction,
            visit_date,
            booking_date,
            "default" if requires_reservation else "none",
            "ready",
        )
        if not changed:
            raise ValueError("未找到可修改的预约计划")
        return self.store.get_reservation_plan(platform, group_id, plan_code)

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
        changed = self.store.set_reservation_draft_item_times(
            platform,
            group_id,
            creator_id,
            plan_code,
            item_index,
            tuple(custom_times),
        )
        if not changed:
            raise ValueError("未找到可设置提醒的预约项目")
        return self.store.get_reservation_plan(platform, group_id, plan_code)

    def confirm_plan(
            self,
            platform: str,
            group_id: str,
            creator_id: str,
            plan_code: str):
        plan = self.store.get_reservation_plan(platform, group_id, plan_code)
        if plan is None:
            raise ValueError("预约计划不存在")
        if plan.creator_id != creator_id:
            raise PermissionError("只有创建者可以确认预约计划")
        reminders = {}
        for item in plan.items:
            if item.requires_reservation:
                if item.status == "needs_input" or item.booking_date is None:
                    raise ValueError("请先补充所有需要预约项目的日期")
                reminders[item.item_id] = build_reminder_occurrences(
                    item.booking_date,
                    item.custom_reminder_times,
                )
        return self.store.confirm_reservation_plan(
            platform,
            group_id,
            creator_id,
            plan_code,
            reminders,
        )

    def list_plans(
            self,
            platform: str,
            group_id: str,
            creator_id: str):
        plans = self.store.list_reservation_plans_for_creator(
            platform,
            group_id,
            creator_id,
        )
        if (
                not plans
                and self.store.group_has_reservation_plans(
                    platform,
                    group_id,
                )):
            raise PermissionError("只有创建者可以查看预约提醒")
        return plans

    def format_plan_list(self, plans: Sequence[object]) -> str:
        if not plans:
            return "当前没有预约提醒"
        lines = []
        for plan in plans:
            lines.append(f"{plan.plan_code}（{plan.status}）")
            for item in plan.items:
                visit = (
                    item.visit_date.isoformat()
                    if item.visit_date
                    else "未定"
                )
                booking = (
                    item.booking_date.isoformat()
                    if item.booking_date
                    else "无需预约"
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
        item = self.store.get_reservation_item(
            platform,
            group_id,
            creator_id,
            public_code,
        )
        if item is None:
            raise PermissionError("只有创建者可以修改预约提醒")
        booking_date = calculate_booking_date(
            visit_date,
            item.advance_value,
            item.advance_unit,
        )
        occurrences = build_reminder_occurrences(
            booking_date,
            item.custom_reminder_times,
        )
        return self.store.replace_reservation_item_schedule(
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
        item = self.store.get_reservation_item(
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
        return self.store.replace_reservation_item_schedule(
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
        result = self.store.cancel_reservation_item(
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
        result = self.store.cancel_reservation_plan(
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
