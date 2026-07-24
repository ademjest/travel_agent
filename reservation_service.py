from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from chat_transport import storage_scope_id
from event_idempotency import event_operation_key


AdvanceUnit = Literal["day", "month", "none"]
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
ABSOLUTE_TIME_FORMAT = "%Y-%m-%d %H:%M"
RESERVATION_WORKFLOW_TTL = timedelta(minutes=30)


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


@dataclass(frozen=True)
class ReservationRefreshResult:
    plan: object
    updated_count: int


ResolutionReason = Literal[
    "resolved",
    "ambiguous",
    "not_scheduled",
    "not_found",
]


@dataclass(frozen=True)
class VisitDateResolution:
    dates: tuple[date, ...]
    reason: ResolutionReason


@dataclass(frozen=True)
class _TripRange:
    start: date
    end: date


@dataclass(frozen=True)
class _DatedItinerarySegment:
    dates: tuple[date, ...]
    lines: tuple[str, ...]


_CHINESE_TRIP_RANGE_RE = re.compile(
    r"(?P<start_year>20\d{2})\s*年\s*"
    r"(?P<start_month>\d{1,2})\s*月\s*"
    r"(?P<start_day>\d{1,2})\s*日?\s*"
    r"(?:—|–|-|~|～|至|到)+\s*"
    r"(?:(?P<end_year>20\d{2})\s*年\s*)?"
    r"(?P<end_month>\d{1,2})\s*月\s*"
    r"(?P<end_day>\d{1,2})\s*日?"
)
_ISO_TRIP_RANGE_RE = re.compile(
    r"(?P<start_year>20\d{2})[-/.]"
    r"(?P<start_month>\d{1,2})[-/.]"
    r"(?P<start_day>\d{1,2})\s*"
    r"(?:—|–|~|～|至|到)+\s*"
    r"(?:(?P<end_year>20\d{2})[-/.])?"
    r"(?P<end_month>\d{1,2})[-/.]"
    r"(?P<end_day>\d{1,2})"
)
_FULL_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*(?:年\s*|[-/.])"
    r"(?P<month>\d{1,2})\s*(?:月\s*|[-/.])"
    r"(?P<day>\d{1,2})\s*日?"
)
_PARTIAL_DATE_RE = re.compile(
    r"(?<![\d年])(?P<month>\d{1,2})\s*月\s*"
    r"(?P<day>\d{1,2})\s*日"
)
_NEGATIVE_VISIT_MARKERS = (
    "不去",
    "不进入",
    "取消",
    "不安排",
    "仅路过",
    "只路过",
    "路过不进",
    "外围经过",
    "经过外围",
    "远观",
    "不游览",
    "不参观",
)
_POSITIVE_ACTIVITY_MARKERS = (
    "游览",
    "参观",
    "前往",
    "到达",
    "进入",
    "打卡",
    "观看",
    "上午",
    "下午",
    "傍晚",
    "重点安排",
)


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _bounded_text(
        payload: Mapping[str, object],
        key: str,
        max_chars: int) -> str:
    value = str(payload.get(key) or "").strip()
    if len(value) > max_chars:
        raise ValueError(f"{key} is too long")
    return value


def normalize_extraction_item(
        payload: Mapping[str, object]) -> ReservationExtractionItem:
    attraction_name = _required_text(payload, "attraction_name")
    if len(attraction_name) > 200:
        raise ValueError("attraction_name is too long")
    requires_reservation = payload.get("requires_reservation")
    if not isinstance(requires_reservation, bool):
        raise ValueError("requires_reservation must be a boolean")
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
        price_text=_bounded_text(payload, "price_text", 500),
        opening_hours=_bounded_text(payload, "opening_hours", 500),
        requires_reservation=requires_reservation,
        advance_value=advance_value,
        advance_unit=advance_unit,
        booking_channel=_bounded_text(payload, "booking_channel", 500),
        source_text=_bounded_text(payload, "source_text", 2_000),
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


class ItineraryResolver(Protocol):
    def resolve(
            self,
            documents: Sequence[object],
            attraction_names: Sequence[str]) -> Mapping[
                str,
                VisitDateResolution,
            ]:
        raise RuntimeError("protocol method")


class ReservationItineraryResolver:
    @staticmethod
    def _safe_date(year: int, month: int, day: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    @classmethod
    def _trip_range(cls, text: str) -> _TripRange | None:
        ranges = set()
        for pattern in (_CHINESE_TRIP_RANGE_RE, _ISO_TRIP_RANGE_RE):
            for match in pattern.finditer(text):
                start_year = int(match.group("start_year"))
                end_year = int(match.group("end_year") or start_year)
                start = cls._safe_date(
                    start_year,
                    int(match.group("start_month")),
                    int(match.group("start_day")),
                )
                end = cls._safe_date(
                    end_year,
                    int(match.group("end_month")),
                    int(match.group("end_day")),
                )
                if start is not None and end is not None and start <= end:
                    ranges.add((start, end))
        if len(ranges) != 1:
            return None
        start, end = next(iter(ranges))
        return _TripRange(start=start, end=end)

    @staticmethod
    def _is_trip_range_line(line: str) -> bool:
        return bool(
            _CHINESE_TRIP_RANGE_RE.search(line)
            or _ISO_TRIP_RANGE_RE.search(line)
        )

    @classmethod
    def _partial_date(
            cls,
            month: int,
            day: int,
            trip_range: _TripRange | None) -> date | None:
        if trip_range is None:
            return None
        candidates = {
            candidate
            for year in range(trip_range.start.year, trip_range.end.year + 1)
            if (candidate := cls._safe_date(year, month, day)) is not None
            and trip_range.start <= candidate <= trip_range.end
        }
        if len(candidates) != 1:
            return None
        return next(iter(candidates))

    @classmethod
    def _line_dates(
            cls,
            line: str,
            trip_range: _TripRange | None) -> tuple[date, ...]:
        if cls._is_trip_range_line(line):
            return ()

        positioned: list[tuple[int, date]] = []
        for match in _FULL_DATE_RE.finditer(line):
            parsed = cls._safe_date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
            if parsed is not None:
                positioned.append((match.start(), parsed))
        for match in _PARTIAL_DATE_RE.finditer(line):
            parsed = cls._partial_date(
                int(match.group("month")),
                int(match.group("day")),
                trip_range,
            )
            if parsed is not None:
                positioned.append((match.start(), parsed))

        if (
                not positioned
                or min(position for position, unused in positioned) > 24):
            return ()
        return tuple(sorted({value for unused, value in positioned}))

    @staticmethod
    def _merge_chunks(chunks: Sequence[str]) -> str:
        merged = ""
        for raw_chunk in chunks:
            chunk = str(raw_chunk or "").strip()
            if not chunk:
                continue
            if not merged:
                merged = chunk
                continue
            overlap = 0
            maximum = min(len(merged), len(chunk), 500)
            for size in range(maximum, 19, -1):
                if merged.endswith(chunk[:size]):
                    overlap = size
                    break
            merged += chunk[overlap:] if overlap else "\n" + chunk
        return merged

    @classmethod
    def _segments(
            cls,
            chunks: Sequence[str]) -> tuple[_DatedItinerarySegment, ...]:
        text = cls._merge_chunks(chunks)
        trip_range = cls._trip_range(text)
        segments = []
        current_dates: tuple[date, ...] = ()
        current_lines: list[str] = []

        for raw_line in text.splitlines():
            line = re.sub(r"[ \t]+", " ", raw_line).strip()
            if not line:
                continue
            dates = cls._line_dates(line, trip_range)
            if dates:
                if current_dates:
                    segments.append(_DatedItinerarySegment(
                        dates=current_dates,
                        lines=tuple(current_lines),
                    ))
                current_dates = dates
                current_lines = [line]
            elif current_dates:
                current_lines.append(line)

        if current_dates:
            segments.append(_DatedItinerarySegment(
                dates=current_dates,
                lines=tuple(current_lines),
            ))
        return tuple(segments)

    @staticmethod
    def _segment_match(
            segment: _DatedItinerarySegment,
            attraction_name: str) -> str:
        matching = [
            (index, line)
            for index, line in enumerate(segment.lines)
            if attraction_name in line
        ]
        if not matching:
            return ""
        positive_seen = False
        for index, line in matching:
            cells = re.split(r"\s*\|\s*", line)
            has_route = any(
                len(re.split(r"(?:→|->)", cell)) > 1
                for cell in cells
            )
            for cell in cells:
                if attraction_name not in cell:
                    continue
                route_parts = re.split(r"(?:→|->)", cell)
                if len(route_parts) > 1:
                    matching_parts = [
                        (position, part)
                        for position, part in enumerate(route_parts)
                        if attraction_name in part
                    ]
                    if any(
                            any(
                                marker in part
                                for marker in _NEGATIVE_VISIT_MARKERS
                            )
                            for unused, part in matching_parts):
                        return "negative"
                    if any(
                            position > 0
                            or any(
                                marker in part
                                for marker in _POSITIVE_ACTIVITY_MARKERS
                            )
                            for position, part in matching_parts):
                        positive_seen = True
                    continue
                statements = [
                    statement
                    for statement in re.split(
                        r"(?:[，,；;。！？!?]+|但是|但|不过|然而|而)",
                        cell,
                    )
                    if attraction_name in statement
                ]
                if any(
                        any(
                            marker in statement
                            for marker in _NEGATIVE_VISIT_MARKERS
                        )
                        for statement in statements):
                    return "negative"
                if any(
                        any(
                            marker in statement
                            for marker in _POSITIVE_ACTIVITY_MARKERS
                        )
                        for statement in statements):
                    positive_seen = True
            if not has_route and index == 0:
                heading_cell = (
                    line
                    if len(cells) == 1
                    else next((cell for cell in cells[1:] if cell), "")
                )
                if attraction_name in heading_cell:
                    positive_seen = True
        return "positive" if positive_seen else ""

    @classmethod
    def _resolve_document(
            cls,
            document: object,
            attraction_names: Sequence[str]) -> dict[
                str,
                VisitDateResolution,
            ]:
        segments = cls._segments(document.chunks)
        resolutions = {}
        for attraction_name in attraction_names:
            positive_dates = set()
            negative_seen = False
            for segment in segments:
                match = cls._segment_match(segment, attraction_name)
                if match == "positive":
                    positive_dates.update(segment.dates)
                elif match == "negative":
                    negative_seen = True

            dates = tuple(sorted(positive_dates))
            if len(dates) == 1:
                reason: ResolutionReason = "resolved"
            elif len(dates) > 1:
                reason = "ambiguous"
            elif negative_seen:
                reason = "not_scheduled"
            else:
                reason = "not_found"
            resolutions[attraction_name] = VisitDateResolution(dates, reason)
        return resolutions

    def resolve(
            self,
            documents: Sequence[object],
            attraction_names: Sequence[str]) -> Mapping[
                str,
                VisitDateResolution,
            ]:
        names = tuple(dict.fromkeys(
            str(name).strip()
            for name in attraction_names
            if str(name).strip()
        ))
        empty = {
            name: VisitDateResolution((), "not_found")
            for name in names
        }
        best_score = 0
        best_resolutions = None
        for document in documents:
            resolutions = self._resolve_document(document, names)
            score = sum(
                1
                for resolution in resolutions.values()
                if resolution.dates
            )
            if score > best_score:
                best_score = score
                best_resolutions = resolutions

        return best_resolutions if best_resolutions is not None else empty


class ReservationService:
    def __init__(
            self,
            store: object,
            itinerary_resolver: ItineraryResolver | None = None):
        self.store = store
        self.itinerary_resolver = (
            itinerary_resolver or ReservationItineraryResolver()
        )

    def start_workflow(
            self,
            platform: str,
            group_id: str,
            creator_id: str) -> None:
        self.store.start_reservation_workflow(
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
        return self.store.reservation_workflow_is_active(
            platform,
            group_id,
            creator_id,
        )

    def finish_workflow(
            self,
            platform: str,
            group_id: str,
            creator_id: str) -> bool:
        return self.store.clear_reservation_workflow(
            platform,
            group_id,
            creator_id,
        )

    def create_draft(
            self,
            image: object,
            extraction_items: Sequence[ReservationExtractionItem],
            now: datetime | None = None,
            source_event_id: str = ""):
        required_names = tuple(dict.fromkeys(
            extraction.attraction_name
            for extraction in extraction_items
            if extraction.requires_reservation
        ))
        resolutions = {}
        if required_names:
            documents = self.store.list_document_contents(
                image.storage_scope_id
            )
            resolutions = self.itinerary_resolver.resolve(
                documents,
                required_names,
            )

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
                "custom_reminder_times": (),
                "reminder_policy": "default",
                "status": status,
            })

        return self.store.create_reservation_draft(
            image_id=image.image_id,
            platform=image.platform,
            group_id=image.group_id,
            creator_id=image.uploader_id,
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
        plan = self.store.get_reservation_plan(platform, group_id, plan_code)
        if plan is None:
            raise ValueError("预约计划不存在")
        if plan.creator_id != creator_id:
            raise PermissionError("只有创建者可以刷新预约计划")
        if plan.status != "draft":
            return ReservationRefreshResult(plan, 0)

        refresh_revision = self.store.claim_reservation_refresh(
            platform,
            group_id,
            creator_id,
            plan_code,
        )
        plan = self.store.get_reservation_plan(platform, group_id, plan_code)
        if plan is None:
            raise RuntimeError("reservation draft disappeared during refresh")
        if refresh_revision is None or plan.status != "draft":
            return ReservationRefreshResult(plan, 0)

        items = tuple(
            item
            for item in plan.items
            if item.requires_reservation
            and item.visit_date is None
            and item.status in {"needs_input", "not_scheduled"}
        )
        if not items:
            return ReservationRefreshResult(plan, 0)

        documents = self.store.list_document_contents(
            storage_scope_id(platform, group_id)
        )
        resolutions = self.itinerary_resolver.resolve(
            documents,
            tuple(item.attraction_name for item in items),
        )
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
                "status": status,
                "refresh_revision": refresh_revision,
            })

        changed = self.store.refresh_reservation_draft_items(
            platform,
            group_id,
            creator_id,
            plan_code,
            tuple(updates),
        )
        refreshed = self.store.get_reservation_plan(
            platform, group_id, plan_code
        )
        if refreshed is None:
            raise RuntimeError("reservation draft disappeared during refresh")
        return ReservationRefreshResult(refreshed, changed)

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
            if item.status == "not_scheduled":
                lines.append("   游览日期：未确定")
                lines.append("   状态：行程未安排该景点，需要手动决定")
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
            source_operation_key=operation_key,
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
        plan = self.refresh_plan(
            platform,
            group_id,
            creator_id,
            plan_code,
        ).plan
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
        cached = self.store.get_event_tool_result(operation_key)
        if cached is not None:
            return cached
        result = self._handle_command_uncached(
            command,
            event,
            operation_key=operation_key,
        )
        return self.store.save_event_tool_result(
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
