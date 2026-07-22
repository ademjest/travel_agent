from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Mapping, Sequence
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
