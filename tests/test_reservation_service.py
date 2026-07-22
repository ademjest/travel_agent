import unittest
from datetime import date, datetime, timezone

from reservation_service import (
    ReservationExtractionItem,
    build_reminder_occurrences,
    calculate_booking_date,
    normalize_extraction_item,
    parse_beijing_datetime_list,
)


class ReservationRuleTests(unittest.TestCase):
    def test_days_are_subtracted_as_natural_beijing_dates(self):
        self.assertEqual(
            calculate_booking_date(date(2026, 8, 16), 3, "day"),
            date(2026, 8, 13),
        )

    def test_month_subtraction_clamps_to_target_month_end(self):
        self.assertEqual(
            calculate_booking_date(date(2026, 3, 31), 1, "month"),
            date(2026, 2, 28),
        )
        self.assertEqual(
            calculate_booking_date(date(2028, 3, 31), 1, "month"),
            date(2028, 2, 29),
        )

    def test_month_subtraction_crosses_year_boundary(self):
        self.assertEqual(
            calculate_booking_date(date(2027, 1, 15), 2, "month"),
            date(2026, 11, 15),
        )

    def test_default_policy_creates_two_utc_occurrences(self):
        occurrences = build_reminder_occurrences(
            booking_date=date(2026, 8, 15),
            custom_times=(),
        )
        self.assertEqual(
            tuple(item.scheduled_at_utc for item in occurrences),
            (
                datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc),
            ),
        )
        self.assertTrue(all(not item.is_custom for item in occurrences))

    def test_custom_policy_replaces_defaults_and_deduplicates(self):
        custom = parse_beijing_datetime_list(
            "2026-08-14 20:00, 2026-08-15 07:30, 2026-08-15 07:30"
        )
        occurrences = build_reminder_occurrences(
            booking_date=date(2026, 8, 15),
            custom_times=custom,
        )
        self.assertEqual(len(occurrences), 2)
        self.assertTrue(all(item.is_custom for item in occurrences))
        self.assertEqual(
            occurrences[1].scheduled_at_utc,
            datetime(2026, 8, 14, 23, 30, tzinfo=timezone.utc),
        )

    def test_no_reservation_wording_normalizes_to_none(self):
        item = normalize_extraction_item({
            "attraction_name": "黑独山",
            "price_text": "",
            "opening_hours": "",
            "requires_reservation": False,
            "advance_value": 0,
            "advance_unit": "none",
            "booking_channel": "",
            "source_text": "无需提前",
            "confidence": 0.98,
        })
        self.assertEqual(
            item,
            ReservationExtractionItem(
                attraction_name="黑独山",
                price_text="",
                opening_hours="",
                requires_reservation=False,
                advance_value=0,
                advance_unit="none",
                booking_channel="",
                source_text="无需提前",
                confidence=0.98,
            ),
        )

    def test_invalid_rule_is_rejected_instead_of_guessed(self):
        with self.assertRaisesRegex(ValueError, "advance_unit"):
            normalize_extraction_item({
                "attraction_name": "莫高窟",
                "requires_reservation": True,
                "advance_value": 1,
                "advance_unit": "week",
                "confidence": 0.9,
            })

    def test_custom_time_requires_complete_absolute_beijing_time(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD HH:MM"):
            parse_beijing_datetime_list("明早七点")


if __name__ == "__main__":
    unittest.main()
