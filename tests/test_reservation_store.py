import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from memory_store import MemoryStore
from reservation_service import ReservationExtractionItem


class ReservationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "reservations.db"
        self.store = MemoryStore(database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_image_deduplication_is_scoped_to_group_storage(self):
        first, first_is_new = self.store.create_reservation_image(
            storage_scope_id="group-a",
            platform="qq_official",
            group_id="group-a",
            uploader_id="member-a",
            sha256="a" * 64,
            file_path="data/images/aa/image.jpg",
            content_type="image/jpeg",
            byte_size=10,
            model_id="vision-model",
        )
        duplicate, duplicate_is_new = self.store.create_reservation_image(
            storage_scope_id="group-a",
            platform="qq_official",
            group_id="group-a",
            uploader_id="member-b",
            sha256="a" * 64,
            file_path="data/images/aa/image.jpg",
            content_type="image/jpeg",
            byte_size=10,
            model_id="vision-model",
        )
        isolated, isolated_is_new = self.store.create_reservation_image(
            storage_scope_id="onebot:group-b",
            platform="onebot",
            group_id="group-b",
            uploader_id="member-c",
            sha256="a" * 64,
            file_path="data/images/aa/image.jpg",
            content_type="image/jpeg",
            byte_size=10,
            model_id="vision-model",
        )
        self.assertTrue(first_is_new)
        self.assertFalse(duplicate_is_new)
        self.assertTrue(isolated_is_new)
        self.assertEqual(first.image_id, duplicate.image_id)
        self.assertNotEqual(first.image_id, isolated.image_id)

    def test_draft_persists_date_candidates_and_custom_times(self):
        image, unused = self.store.create_reservation_image(
            storage_scope_id="group-a",
            platform="qq_official",
            group_id="group-a",
            uploader_id="member-a",
            sha256="b" * 64,
            file_path="data/images/bb/image.png",
            content_type="image/png",
            byte_size=20,
            model_id="vision-model",
        )
        plan = self.store.create_reservation_draft(
            image_id=image.image_id,
            platform="qq_official",
            group_id="group-a",
            creator_id="member-a",
            items=(
                {
                    "extraction": ReservationExtractionItem(
                        attraction_name="莫高窟",
                        price_text="238元",
                        opening_hours="08:00-18:00",
                        requires_reservation=True,
                        advance_value=1,
                        advance_unit="month",
                        booking_channel="官方小程序",
                        source_text="莫高窟提前一个月预约",
                        confidence=0.93,
                    ),
                    "visit_date": None,
                    "booking_date": None,
                    "date_candidates": (
                        date(2026, 8, 20),
                        date(2026, 8, 21),
                    ),
                    "custom_reminder_times": (
                        datetime(
                            2026,
                            7,
                            20,
                            12,
                            tzinfo=timezone.utc,
                        ),
                    ),
                    "reminder_policy": "custom",
                    "status": "needs_input",
                },
            ),
            now=datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc),
        )
        loaded = self.store.get_reservation_plan(
            "qq_official",
            "group-a",
            plan.plan_code,
        )
        self.assertEqual(
            loaded.items[0].date_candidates,
            (date(2026, 8, 20), date(2026, 8, 21)),
        )
        self.assertEqual(
            loaded.items[0].custom_reminder_times[0],
            datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        )

    def test_plan_lookup_cannot_cross_group_boundary(self):
        image, unused = self.store.create_reservation_image(
            storage_scope_id="group-a",
            platform="qq_official",
            group_id="group-a",
            uploader_id="member-a",
            sha256="c" * 64,
            file_path="data/images/cc/image.webp",
            content_type="image/webp",
            byte_size=30,
            model_id="vision-model",
        )
        plan = self.store.create_reservation_draft(
            image_id=image.image_id,
            platform="qq_official",
            group_id="group-a",
            creator_id="member-a",
            items=(),
        )
        self.assertIsNone(
            self.store.get_reservation_plan(
                "qq_official",
                "group-b",
                plan.plan_code,
            )
        )


if __name__ == "__main__":
    unittest.main()
