import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.data_paths import app_data_dir, database_path, image_root, log_directory
from infrastructure.memory_store import MemoryStore
from services.vision_service import ReservationImageService


class DataPathTests(unittest.TestCase):
    def test_logs_are_beside_project_and_independent_of_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'travel_agent'
            with patch('core.data_paths.PROJECT_ROOT', root), patch.dict(os.environ, {'APP_DATA_DIR': 'elsewhere'}):
                self.assertEqual(log_directory(), root.parent / 'travel_agent_val_log')

    def test_app_data_dir_controls_database_and_images(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                    os.environ,
                    {"APP_DATA_DIR": directory},
                    clear=False):
                expected = Path(directory).resolve()

                self.assertEqual(app_data_dir(), expected)
                self.assertEqual(database_path(), expected / "travel_bot.db")
                self.assertEqual(image_root(), expected / "images")
                self.assertEqual(
                    MemoryStore().database_path,
                    expected / "travel_bot.db",
                )
                self.assertEqual(
                    ReservationImageService(
                        MemoryStore(expected / "other.db"),
                        None,
                    ).image_root,
                    expected / "images",
                )


if __name__ == "__main__":
    unittest.main()
