import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maintenance import MaintenanceService, RetentionPolicy
from memory_store import MemoryStore


class MaintenanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = MemoryStore(root / "memory.db")
        self.image_root = root / "images"
        self.image_root.mkdir()
        self.now = datetime(2026, 7, 24, tzinfo=timezone.utc)
        self.service = MaintenanceService(
            self.store,
            self.image_root,
            RetentionPolicy(
                chat_days=30,
                event_days=30,
                binding_days=7,
                reservation_days=90,
                image_days=90,
                document_days=0,
            ),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_terminal_events_bindings_and_orphan_images_are_purged(self):
        old = self.now - timedelta(days=100)
        claim = self.store.begin_event("old-event", now=old)
        self.store.complete_event(
            claim.event_id,
            claim.claim_token,
            now=old,
        )
        self.store.create_upload_binding(
            code_hash="old-binding",
            group_openid="group-a",
            issuer_openid="member-a",
            expires_at=old,
            now=old,
        )
        image_path = self.image_root / "old.jpg"
        image_path.write_bytes(b"image")
        self.store.create_reservation_image(
            storage_scope_id="scope-a",
            platform="qq_official",
            group_id="group-a",
            uploader_id="member-a",
            sha256="a" * 64,
            file_path=str(image_path),
            content_type="image/jpeg",
            byte_size=5,
            model_id="vision",
            now=old,
        )

        result = self.service.run_once(self.now)

        self.assertEqual(result["processed_events"], 1)
        self.assertEqual(result["upload_bindings"], 1)
        self.assertEqual(result["reservation_images"], 1)
        self.assertEqual(result["image_files"], 1)
        self.assertFalse(image_path.exists())

    def test_document_retention_is_disabled_by_default(self):
        old = self.now - timedelta(days=400)
        self.store.add_document(
            "group-a",
            "member-a",
            "trip.txt",
            "hash",
            "青海湖旅行计划正文",
            ["青海湖旅行计划正文"],
        )
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE documents SET created_at = ?",
                (old.isoformat(),),
            )

        result = self.service.run_once(self.now)

        self.assertEqual(result["documents"], 0)
        self.assertTrue(self.store.list_document_contents("group-a"))


if __name__ == "__main__":
    unittest.main()
