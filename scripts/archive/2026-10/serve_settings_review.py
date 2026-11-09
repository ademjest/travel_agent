"""Temporary UI review server; writes only fixture credentials to a temporary .env."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from adapters.web_app import create_web_app
from core.settings import Settings
from core.web_settings import WebSettings
from infrastructure.env_settings_store import EnvSettingsStore


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='travel-settings-review-') as directory:
        root = Path(directory)
        (root / '.env').write_text('LLM_API_KEY=model-fixture\nAMAP_API_KEY=map-fixture\n', encoding='utf-8')
        app = create_web_app(WebSettings(port=8094, data_dir=root/'data', open_browser=False),
            Settings('', '', frozenset(), '', '', '', ''), settings_store=EnvSettingsStore(root, inherited={}))
        uvicorn.run(app, host='127.0.0.1', port=8094)
