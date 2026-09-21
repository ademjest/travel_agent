import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def app_data_dir() -> Path:
    configured = os.getenv("APP_DATA_DIR", "").strip()
    path = Path(configured) if configured else PROJECT_ROOT / "data"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def database_path() -> Path:
    return app_data_dir() / "travel_bot.db"


def image_root() -> Path:
    return app_data_dir() / "images"
