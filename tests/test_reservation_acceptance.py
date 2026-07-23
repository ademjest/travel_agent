import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from memory_store import MemoryStore
from reservation_service import (
    ReservationService,
    calculate_booking_date,
    normalize_extraction_item,
)


class ReservationAcceptanceTests(unittest.TestCase):
    def test_sample_image_creates_ten_items_and_sixteen_confirmed_reminders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "acceptance.db")
            fixture_path = (
                Path(__file__).parent
                / "fixtures"
                / "reservation_image_extraction.json"
            )
            payload = json.loads(fixture_path.read_text(encoding="utf-8"))
            extracted_items = tuple(
                normalize_extraction_item(item)
                for item in payload["items"]
            )
            image, unused = store.create_reservation_image(
                storage_scope_id="group-a",
                platform="qq_official",
                group_id="group-a",
                uploader_id="member-a",
                sha256="f" * 64,
                file_path="data/images/ff/sample.jpg",
                content_type="image/jpeg",
                byte_size=100,
                model_id="fake-model",
            )
            required_names = {
                item.attraction_name
                for item in extracted_items
                if item.requires_reservation
            }
            visit_dates = {
                name: date(2026, 8, 10 + index)
                for index, name in enumerate(sorted(required_names))
            }
            itinerary_text = "\n".join(
                f"{visit_date.isoformat()}｜游览{name}。"
                for name, visit_date in sorted(visit_dates.items())
            )
            store.add_document(
                group_openid="group-a",
                uploader_openid="member-a",
                filename="完整行程.md",
                sha256="combined-itinerary",
                full_text=itinerary_text,
                chunks=[itinerary_text],
            )
            service = ReservationService(store)
            draft = service.create_draft(image, extracted_items)

            self.assertEqual(len(draft.items), 10)
            self.assertEqual(
                sum(item.requires_reservation for item in draft.items),
                8,
            )
            self.assertEqual(
                {
                    item.attraction_name
                    for item in draft.items
                    if not item.requires_reservation
                },
                {"日月山", "黑独山"},
            )
            self.assertEqual(
                store.list_reservation_reminders(
                    "qq_official",
                    "group-a",
                    "member-a",
                ),
                (),
            )

            confirmed = service.confirm_plan(
                "qq_official",
                "group-a",
                "member-a",
                draft.plan_code,
            )
            reminders = store.list_reservation_reminders(
                "qq_official",
                "group-a",
                "member-a",
            )

            self.assertEqual(confirmed.status, "confirmed")
            self.assertEqual(len(reminders), 16)
            mogao = next(
                item
                for item in confirmed.items
                if item.attraction_name == "莫高窟"
            )
            self.assertEqual(
                mogao.booking_date,
                calculate_booking_date(mogao.visit_date, 1, "month"),
            )


if __name__ == "__main__":
    unittest.main()
