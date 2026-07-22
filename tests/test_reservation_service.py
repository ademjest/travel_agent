import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from memory_store import MemoryStore
from reservation_service import (
    LLMVisitDateExtractor,
    ReservationExtractionItem,
    ReservationService,
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


class FakeDateExtractor:
    def __init__(self, dates_by_attraction):
        self.dates_by_attraction = dates_by_attraction
        self.calls = []

    def extract(self, attraction_name, evidence):
        self.calls.append((attraction_name, evidence))
        return self.dates_by_attraction.get(attraction_name, ())


class DateResponseClient:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    def create(self, **request):
        self.calls.append(request)
        content = self.contents.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content)
                )
            ]
        )


class ReservationDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "drafts.db")
        self.image, unused = self.store.create_reservation_image(
            storage_scope_id="group-a",
            platform="qq_official",
            group_id="group-a",
            uploader_id="member-a",
            sha256="d" * 64,
            file_path="data/images/dd/image.jpg",
            content_type="image/jpeg",
            byte_size=100,
            model_id="vision-model",
        )
        self.store.add_document(
            group_openid="group-a",
            uploader_openid="member-a",
            filename="行程.md",
            sha256="trip-document",
            full_text=(
                "2026-08-16 游览青海湖。\n"
                "2026-08-20 或 2026-08-21 游览莫高窟。"
            ),
            chunks=[
                "2026-08-16 游览青海湖。",
                "2026-08-20 或 2026-08-21 游览莫高窟。",
            ],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def item(name, requires, value, unit, confidence=0.95):
        return ReservationExtractionItem(
            attraction_name=name,
            price_text="",
            opening_hours="",
            requires_reservation=requires,
            advance_value=value,
            advance_unit=unit,
            booking_channel="",
            source_text=name,
            confidence=confidence,
        )

    def test_unique_document_date_becomes_ready(self):
        service = ReservationService(
            self.store,
            FakeDateExtractor({
                "青海湖": (date(2026, 8, 16),),
            }),
        )
        plan = service.create_draft(
            self.image,
            (self.item("青海湖", True, 1, "day"),),
        )
        self.assertEqual(plan.items[0].status, "ready")
        self.assertEqual(plan.items[0].visit_date, date(2026, 8, 16))
        self.assertEqual(plan.items[0].booking_date, date(2026, 8, 15))
        self.assertLessEqual(len(service.date_extractor.calls[0][1]), 1600)

    def test_zero_or_multiple_dates_require_manual_input(self):
        service = ReservationService(
            self.store,
            FakeDateExtractor({
                "莫高窟": (
                    date(2026, 8, 20),
                    date(2026, 8, 21),
                ),
            }),
        )
        plan = service.create_draft(
            self.image,
            (
                self.item("莫高窟", True, 1, "month"),
                self.item("翡翠湖", True, 3, "day"),
            ),
        )
        self.assertEqual(
            tuple(item.status for item in plan.items),
            ("needs_input", "needs_input"),
        )
        self.assertEqual(len(plan.items[0].date_candidates), 2)

    def test_no_reservation_item_skips_date_matching(self):
        extractor = FakeDateExtractor({})
        service = ReservationService(self.store, extractor)
        plan = service.create_draft(
            self.image,
            (self.item("黑独山", False, 0, "none"),),
        )
        self.assertEqual(plan.items[0].status, "ready")
        self.assertEqual(plan.items[0].reminder_policy, "none")
        self.assertEqual(extractor.calls, [])

    def test_empty_extraction_still_creates_manual_draft(self):
        service = ReservationService(self.store, FakeDateExtractor({}))
        plan = service.create_draft(self.image, ())
        self.assertEqual(plan.items, ())
        reply = service.format_draft(plan)
        self.assertIn("新增预约", reply)

    def test_manual_add_and_date_completion_recalculate_booking_date(self):
        service = ReservationService(self.store, FakeDateExtractor({}))
        plan = service.create_draft(self.image, ())
        added = service.add_manual_item(
            platform="qq_official",
            group_id="group-a",
            creator_id="member-a",
            plan_code=plan.plan_code,
            attraction_name="莫高窟",
            visit_date=date(2026, 8, 20),
            advance_value=1,
            advance_unit="month",
            requires_reservation=True,
        )
        self.assertEqual(added.items[0].booking_date, date(2026, 7, 20))

        incomplete = service.create_draft(
            self.image,
            (self.item("翡翠湖", True, 3, "day"),),
        )
        completed = service.complete_item_date(
            "qq_official",
            "group-a",
            "member-a",
            incomplete.plan_code,
            1,
            date(2026, 8, 18),
        )
        self.assertEqual(completed.items[0].status, "ready")
        self.assertEqual(completed.items[0].booking_date, date(2026, 8, 15))

    def test_llm_date_extractor_rejects_incomplete_year(self):
        client = DateResponseClient(['{"dates":["08-20"]}'])
        extractor = LLMVisitDateExtractor("model", client)
        self.assertEqual(
            extractor.extract("莫高窟", "8月20日游览莫高窟"),
            (),
        )

    def test_llm_date_extractor_accepts_only_complete_iso_dates(self):
        client = DateResponseClient([
            '{"dates":["2026-08-20","2026-08-20"]}'
        ])
        extractor = LLMVisitDateExtractor("model", client)
        self.assertEqual(
            extractor.extract("莫高窟", "2026-08-20 游览莫高窟"),
            (date(2026, 8, 20),),
        )
        user_content = client.calls[0]["messages"][1]["content"]
        self.assertIn("<untrusted_itinerary>", user_content)


if __name__ == "__main__":
    unittest.main()
