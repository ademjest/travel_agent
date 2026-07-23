import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from memory_store import MemoryStore, StoredDocumentContent
from reservation_service import (
    ReservationExtractionItem,
    ReservationItineraryResolver,
    ReservationService,
    VisitDateResolution,
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


class FakeItineraryResolver:
    def __init__(self, resolutions):
        self.resolutions = resolutions
        self.calls = []

    def resolve(self, documents, attraction_names):
        self.calls.append((tuple(documents), tuple(attraction_names)))
        return {
            name: self.resolutions.get(
                name,
                VisitDateResolution((), "not_found"),
            )
            for name in attraction_names
        }


class ReservationItineraryResolverTests(unittest.TestCase):
    def test_resolves_qinghai_daily_dates_without_using_trip_start(self):
        document = StoredDocumentContent(
            document_id=1,
            filename="青甘七日自驾行程更新版V3.docx",
            chunks=(
                (
                    "旅行日期：2026年8月16日—8月22日\n"
                    "8月16日｜曹家堡机场 → 西宁市区酒店；"
                    "当天不去青海湖或茶卡盐湖。\n"
                    "8月17日｜西宁 → 日月山 → 青海湖 → "
                    "茶卡盐湖 → 都兰。\n"
                    "8月18日｜都兰 → 察尔汗盐湖 → 大柴旦。\n"
                    "8月19日｜大柴旦 → 翡翠湖 → 黑独山 → 敦煌。\n"
                    "8月20日｜上午参观莫高窟，傍晚游览鸣沙山月牙泉。\n"
                    "8月21日｜敦煌 → 嘉峪关外围经过，但不进入关城 → 张掖。"
                ),
            ),
        )
        resolver = ReservationItineraryResolver()

        resolutions = resolver.resolve(
            (document,),
            (
                "青海湖",
                "茶卡盐湖",
                "察尔汗盐湖",
                "翡翠湖",
                "莫高窟",
                "鸣沙山",
                "嘉峪关",
                "水上雅丹",
            ),
        )

        self.assertEqual(resolutions["青海湖"].dates, (date(2026, 8, 17),))
        self.assertEqual(resolutions["茶卡盐湖"].dates, (date(2026, 8, 17),))
        self.assertEqual(resolutions["察尔汗盐湖"].dates, (date(2026, 8, 18),))
        self.assertEqual(resolutions["翡翠湖"].dates, (date(2026, 8, 19),))
        self.assertEqual(resolutions["莫高窟"].dates, (date(2026, 8, 20),))
        self.assertEqual(resolutions["鸣沙山"].dates, (date(2026, 8, 20),))
        self.assertEqual(resolutions["嘉峪关"].reason, "not_scheduled")
        self.assertEqual(resolutions["水上雅丹"].reason, "not_found")
        self.assertNotIn(
            date(2026, 8, 16),
            tuple(
                candidate
                for resolution in resolutions.values()
                for candidate in resolution.dates
            ),
        )

    def test_keeps_multiple_positive_dates_for_manual_confirmation(self):
        document = StoredDocumentContent(
            document_id=1,
            filename="敦煌安排.md",
            chunks=(
                "旅行日期：2026年8月16日—8月22日\n"
                "8月20日或8月21日｜参观莫高窟。",
            ),
        )

        resolution = ReservationItineraryResolver().resolve(
            (document,),
            ("莫高窟",),
        )["莫高窟"]

        self.assertEqual(
            resolution.dates,
            (date(2026, 8, 20), date(2026, 8, 21)),
        )
        self.assertEqual(resolution.reason, "ambiguous")

    def test_partial_date_without_explicit_trip_range_is_not_inferred(self):
        document = StoredDocumentContent(
            document_id=1,
            filename="无年份.md",
            chunks=("8月20日｜参观莫高窟。",),
        )

        resolution = ReservationItineraryResolver().resolve(
            (document,),
            ("莫高窟",),
        )["莫高窟"]

        self.assertEqual(resolution, VisitDateResolution((), "not_found"))

    def test_overlapping_chunks_do_not_duplicate_date_candidates(self):
        overlap = "8月20日｜上午参观莫高窟，傍晚游览鸣沙山。"
        document = StoredDocumentContent(
            document_id=1,
            filename="重叠.md",
            chunks=(
                "旅行日期：2026年8月16日—8月22日\n" + overlap,
                overlap + "\n8月21日｜前往张掖。",
            ),
        )

        resolution = ReservationItineraryResolver().resolve(
            (document,),
            ("莫高窟",),
        )["莫高窟"]

        self.assertEqual(resolution.dates, (date(2026, 8, 20),))

    def test_undated_booking_policy_does_not_inherit_previous_day(self):
        document = StoredDocumentContent(
            document_id=1,
            filename="预约说明.md",
            chunks=(
                "旅行日期：2026年8月16日—8月22日\n"
                "8月20日｜敦煌市内休整。\n"
                "预约说明：水上雅丹提前3天预约。",
            ),
        )

        resolution = ReservationItineraryResolver().resolve(
            (document,),
            ("水上雅丹",),
        )["水上雅丹"]

        self.assertEqual(resolution, VisitDateResolution((), "not_found"))

    def test_selects_highest_coverage_document_without_cross_filling(self):
        newer = StoredDocumentContent(
            document_id=2,
            filename="newer.md",
            chunks=(
                "2026-08-19｜前往翡翠湖。",
            ),
        )
        older = StoredDocumentContent(
            document_id=1,
            filename="older.md",
            chunks=(
                "2026-08-17｜游览青海湖。\n"
                "2026-08-20｜参观莫高窟。",
            ),
        )

        resolutions = ReservationItineraryResolver().resolve(
            (newer, older),
            ("青海湖", "莫高窟", "翡翠湖"),
        )

        self.assertEqual(resolutions["青海湖"].dates, (date(2026, 8, 17),))
        self.assertEqual(resolutions["莫高窟"].dates, (date(2026, 8, 20),))
        self.assertEqual(resolutions["翡翠湖"].reason, "not_found")

    def test_equal_coverage_prefers_newest_document(self):
        newer = StoredDocumentContent(
            document_id=2,
            filename="newer.md",
            chunks=("2026-08-19｜前往翡翠湖。",),
        )
        older = StoredDocumentContent(
            document_id=1,
            filename="older.md",
            chunks=("2026-08-17｜游览青海湖。",),
        )

        resolutions = ReservationItineraryResolver().resolve(
            (newer, older),
            ("青海湖", "翡翠湖"),
        )

        self.assertEqual(resolutions["翡翠湖"].dates, (date(2026, 8, 19),))
        self.assertEqual(resolutions["青海湖"].reason, "not_found")

    def test_all_zero_scores_select_no_document(self):
        document = StoredDocumentContent(
            document_id=1,
            filename="pass-by.md",
            chunks=(
                "2026-08-21｜嘉峪关外围经过，但不进入关城。",
            ),
        )

        resolution = ReservationItineraryResolver().resolve(
            (document,),
            ("嘉峪关",),
        )["嘉峪关"]

        self.assertEqual(resolution, VisitDateResolution((), "not_found"))


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
        resolver = FakeItineraryResolver({
            "青海湖": VisitDateResolution(
                (date(2026, 8, 16),),
                "resolved",
            ),
        })
        service = ReservationService(
            self.store,
            itinerary_resolver=resolver,
        )
        plan = service.create_draft(
            self.image,
            (self.item("青海湖", True, 1, "day"),),
        )
        self.assertEqual(plan.items[0].status, "ready")
        self.assertEqual(plan.items[0].visit_date, date(2026, 8, 16))
        self.assertEqual(plan.items[0].booking_date, date(2026, 8, 15))
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0][1], ("青海湖",))
        self.assertEqual(resolver.calls[0][0][0].filename, "行程.md")

    def test_zero_or_multiple_dates_require_manual_input(self):
        service = ReservationService(
            self.store,
            itinerary_resolver=FakeItineraryResolver({
                "莫高窟": VisitDateResolution(
                    (date(2026, 8, 20), date(2026, 8, 21)),
                    "ambiguous",
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
        resolver = FakeItineraryResolver({})
        service = ReservationService(
            self.store,
            itinerary_resolver=resolver,
        )
        plan = service.create_draft(
            self.image,
            (self.item("黑独山", False, 0, "none"),),
        )
        self.assertEqual(plan.items[0].status, "ready")
        self.assertEqual(plan.items[0].reminder_policy, "none")
        self.assertEqual(resolver.calls, [])

    def test_empty_extraction_still_creates_manual_draft(self):
        service = ReservationService(
            self.store,
            itinerary_resolver=FakeItineraryResolver({}),
        )
        plan = service.create_draft(self.image, ())
        self.assertEqual(plan.items, ())
        reply = service.format_draft(plan)
        self.assertIn("新增预约", reply)

    def test_manual_add_and_date_completion_recalculate_booking_date(self):
        service = ReservationService(
            self.store,
            itinerary_resolver=FakeItineraryResolver({}),
        )
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

    def test_not_scheduled_item_uses_explicit_manual_decision_status(self):
        resolver = FakeItineraryResolver({
            "嘉峪关": VisitDateResolution((), "not_scheduled"),
        })
        service = ReservationService(
            self.store,
            itinerary_resolver=resolver,
        )

        plan = service.create_draft(
            self.image,
            (self.item("嘉峪关", True, 1, "day"),),
        )

        self.assertEqual(plan.items[0].status, "not_scheduled")
        self.assertIn(
            "行程未安排该景点，需要手动决定",
            service.format_draft(plan),
        )


class ReservationManagementTests(ReservationDraftTests):
    def ready_plan(self, custom_times=()):
        service = ReservationService(
            self.store,
            itinerary_resolver=FakeItineraryResolver({
                "青海湖": VisitDateResolution(
                    (date(2026, 8, 16),),
                    "resolved",
                ),
            }),
        )
        plan = service.create_draft(
            self.image,
            (
                self.item("青海湖", True, 1, "day"),
                self.item("黑独山", False, 0, "none"),
            ),
        )
        if custom_times:
            plan = service.set_draft_reminder_times(
                "qq_official",
                "group-a",
                "member-a",
                plan.plan_code,
                1,
                custom_times,
            )
        return service, plan

    def test_default_confirmation_creates_two_reminders_only_for_required_item(self):
        service, plan = self.ready_plan()
        confirmed = service.confirm_plan(
            "qq_official",
            "group-a",
            "member-a",
            plan.plan_code,
        )
        reminders = self.store.list_reservation_reminders(
            "qq_official",
            "group-a",
            "member-a",
        )
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(len(reminders), 2)
        self.assertTrue(all(not item.is_custom for item in reminders))

    def test_custom_times_replace_both_defaults(self):
        custom = parse_beijing_datetime_list(
            "2026-08-14 18:30, 2026-08-15 07:00"
        )
        service, plan = self.ready_plan(custom)
        service.confirm_plan(
            "qq_official",
            "group-a",
            "member-a",
            plan.plan_code,
        )
        reminders = self.store.list_reservation_reminders(
            "qq_official",
            "group-a",
            "member-a",
        )
        self.assertEqual(len(reminders), 2)
        self.assertTrue(all(item.is_custom for item in reminders))

    def test_all_no_reservation_plan_confirms_with_zero_reminders(self):
        service = ReservationService(
            self.store,
            itinerary_resolver=FakeItineraryResolver({}),
        )
        plan = service.create_draft(
            self.image,
            (self.item("黑独山", False, 0, "none"),),
        )
        confirmed = service.confirm_plan(
            "qq_official",
            "group-a",
            "member-a",
            plan.plan_code,
        )
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(
            self.store.list_reservation_reminders(
                "qq_official", "group-a", "member-a"
            ),
            (),
        )

    def test_incomplete_plan_cannot_be_confirmed(self):
        service = ReservationService(
            self.store,
            itinerary_resolver=FakeItineraryResolver({}),
        )
        plan = service.create_draft(
            self.image,
            (self.item("翡翠湖", True, 3, "day"),),
        )
        with self.assertRaisesRegex(ValueError, "补充"):
            service.confirm_plan(
                "qq_official",
                "group-a",
                "member-a",
                plan.plan_code,
            )
        self.assertEqual(
            self.store.list_reservation_reminders(
                "qq_official", "group-a", "member-a"
            ),
            (),
        )

    def test_repeated_confirmation_is_idempotent(self):
        service, plan = self.ready_plan()
        first = service.confirm_plan(
            "qq_official", "group-a", "member-a", plan.plan_code
        )
        second = service.confirm_plan(
            "qq_official", "group-a", "member-a", plan.plan_code
        )
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(
            len(self.store.list_reservation_reminders(
                "qq_official", "group-a", "member-a"
            )),
            2,
        )

    def test_non_creator_cannot_view_modify_or_cancel(self):
        service, plan = self.ready_plan()
        confirmed = service.confirm_plan(
            "qq_official", "group-a", "member-a", plan.plan_code
        )
        item_code = confirmed.items[0].public_code
        with self.assertRaisesRegex(PermissionError, "创建者"):
            service.list_plans("qq_official", "group-a", "member-b")
        with self.assertRaisesRegex(PermissionError, "创建者"):
            service.modify_item_date(
                "qq_official",
                "group-a",
                "member-b",
                item_code,
                date(2026, 8, 17),
            )
        with self.assertRaisesRegex(PermissionError, "创建者"):
            service.cancel_item(
                "qq_official", "group-a", "member-b", item_code
            )

    def test_modifying_visit_date_replaces_unsent_reminders(self):
        service, plan = self.ready_plan()
        confirmed = service.confirm_plan(
            "qq_official", "group-a", "member-a", plan.plan_code
        )
        item_code = confirmed.items[0].public_code
        result = service.modify_item_date(
            "qq_official",
            "group-a",
            "member-a",
            item_code,
            date(2026, 8, 18),
        )
        active = self.store.list_reservation_reminders(
            "qq_official", "group-a", "member-a"
        )
        self.assertEqual(result.item.visit_date, date(2026, 8, 18))
        self.assertEqual(len(active), 2)
        self.assertEqual({item.status for item in active}, {"pending"})

    def test_modifying_confirmed_times_replaces_default_set(self):
        service, plan = self.ready_plan()
        confirmed = service.confirm_plan(
            "qq_official", "group-a", "member-a", plan.plan_code
        )
        result = service.modify_item_times(
            "qq_official",
            "group-a",
            "member-a",
            confirmed.items[0].public_code,
            parse_beijing_datetime_list("2026-08-15 07:30"),
        )
        active = self.store.list_reservation_reminders(
            "qq_official", "group-a", "member-a"
        )
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0].is_custom)
        self.assertEqual(result.item.reminder_policy, "custom")

    def test_cancelled_item_has_no_active_reminders(self):
        service, plan = self.ready_plan()
        confirmed = service.confirm_plan(
            "qq_official", "group-a", "member-a", plan.plan_code
        )
        service.cancel_item(
            "qq_official",
            "group-a",
            "member-a",
            confirmed.items[0].public_code,
        )
        self.assertEqual(
            self.store.list_reservation_reminders(
                "qq_official", "group-a", "member-a"
            ),
            (),
        )

    def test_cancel_plan_cancels_every_item_and_reminder(self):
        service, plan = self.ready_plan()
        service.confirm_plan(
            "qq_official", "group-a", "member-a", plan.plan_code
        )
        warning = service.cancel_plan(
            "qq_official",
            "group-a",
            "member-a",
            plan.plan_code,
        )
        self.assertFalse(warning)
        cancelled = self.store.get_reservation_plan(
            "qq_official", "group-a", plan.plan_code
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertTrue(
            all(item.status == "cancelled" for item in cancelled.items)
        )


if __name__ == "__main__":
    unittest.main()
