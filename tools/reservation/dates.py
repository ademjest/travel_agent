from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Mapping, Protocol, Sequence
from core.time_input import BEIJING as BEIJING_TZ



AdvanceUnit = Literal["day", "month", "none"]


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
    source: dict = field(default_factory=dict, compare=False)


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
_TIMELINE_EVENT_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}\s*(?P<detail>.+)$"
)
_TIMELINE_ARRIVAL_RE = re.compile(
    r"(?:到达|抵达|到|前往)[^，,；;。！？!?]{0,40}$"
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
    def _timeline_event_match(text: str, attraction_name: str) -> str:
        match = _TIMELINE_EVENT_RE.match(text)
        if match is None:
            return ""
        detail = match.group("detail")
        attraction_position = detail.find(attraction_name)
        if attraction_position < 0:
            return ""
        if any(marker in detail for marker in _NEGATIVE_VISIT_MARKERS):
            return "negative"
        if _TIMELINE_ARRIVAL_RE.search(detail[:attraction_position]):
            return "positive"
        if any(marker in detail for marker in _POSITIVE_ACTIVITY_MARKERS):
            return "positive"
        return ""

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
                timeline_match = (
                    ReservationItineraryResolver._timeline_event_match(
                        cell,
                        attraction_name,
                    )
                )
                if timeline_match == "negative":
                    return "negative"
                if timeline_match == "positive":
                    positive_seen = True
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
            evidence = []
            negative_seen = False
            for segment in segments:
                match = cls._segment_match(segment, attraction_name)
                if match == "positive":
                    positive_dates.update(segment.dates)
                    evidence.extend(segment.lines)
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
            resolutions[attraction_name] = VisitDateResolution(dates, reason, {
                "origin": "document", "document_id": document.document_id,
                "filename": document.filename, "evidence": "\n".join(evidence)[:1000],
            })
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


