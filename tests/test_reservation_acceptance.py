import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook

from document_service import DocumentService
from memory_store import MemoryStore
from reservation_service import (
    ReservationService,
    calculate_booking_date,
    normalize_extraction_item,
)


def make_acceptance_xlsx():
    buffer = io.BytesIO()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "每日行程"
    worksheet.append(["日期", "行程"])
    worksheet.append([
        date(2026, 8, 17),
        "西宁 → 青海湖 → 茶卡盐湖 → 都兰",
    ])
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


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

    def test_xlsx_upload_is_queryable_and_drives_reservation_dates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "xlsx-acceptance.db")
            documents = DocumentService(store)
            attachment = SimpleNamespace(
                filename="青甘行程.xlsx",
                url="https://example.test/青甘行程.xlsx",
                size=100,
            )
            with patch.object(
                    documents,
                    "_download_attachment",
                    return_value=make_acceptance_xlsx()):
                ingest = documents.ingest_attachments(
                    "group-xlsx",
                    "member-a",
                    [attachment],
                )

            self.assertTrue(ingest.handled)
            self.assertIn(
                "茶卡盐湖",
                store.build_document_context("group-xlsx", "茶卡盐湖"),
            )

            image, unused = store.create_reservation_image(
                storage_scope_id="group-xlsx",
                platform="qq_official",
                group_id="group-xlsx",
                uploader_id="member-a",
                sha256="e" * 64,
                file_path="data/images/ee/xlsx.jpg",
                content_type="image/jpeg",
                byte_size=100,
                model_id="fake-model",
            )
            items = tuple(
                normalize_extraction_item({
                    "attraction_name": name,
                    "requires_reservation": True,
                    "advance_value": 1,
                    "advance_unit": "day",
                    "confidence": 0.99,
                })
                for name in ("青海湖", "茶卡盐湖")
            )

            draft = ReservationService(store).create_draft(image, items)

            self.assertEqual(
                tuple(item.visit_date for item in draft.items),
                (date(2026, 8, 17), date(2026, 8, 17)),
            )
            self.assertEqual(
                tuple(item.booking_date for item in draft.items),
                (date(2026, 8, 16), date(2026, 8, 16)),
            )


if __name__ == "__main__":
    unittest.main()
