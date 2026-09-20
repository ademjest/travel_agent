from dataclasses import dataclass
from contextlib import contextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from infrastructure.env_settings_store import START_ENV  # capture inherited environment before dotenv loading

from core.data_paths import PROJECT_ROOT, database_path
from core.settings import Settings, SettingsError


@contextmanager
def web_data_lease(root):
    """One scheduler per data directory, even when a second server uses another port."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.web-runtime.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SettingsError('这个 Web 数据目录已经有服务在运行，请使用已有网页。') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class WebSettings:
    host: str = '127.0.0.1'
    port: int = 8080
    data_dir: Path = PROJECT_ROOT / 'data' / 'web'
    open_browser: bool = True
    dev: bool = False

    def validate(self):
        if self.host not in {'127.0.0.1', 'localhost'}:
            raise SettingsError('网页端目前仅支持本机访问，WEB_HOST 请使用 127.0.0.1。')
        if not 1 <= self.port <= 65535:
            raise SettingsError('WEB_PORT 必须在 1 到 65535 之间。')
        actual = (self.data_dir / 'travel_bot.db').resolve()
        if actual in {database_path().resolve(), (PROJECT_ROOT / 'data' / 'travel_bot.db').resolve()}:
            raise SettingsError('Web 必须使用独立数据目录，不能打开 QQ 数据库。')

    @classmethod
    def from_env(cls):
        load_dotenv(PROJECT_ROOT / '.env', override=False)
        load_dotenv(PROJECT_ROOT.parent / '.env', override=False)
        root = Path(os.getenv('WEB_DATA_DIR', 'data/web'))
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        try:
            port = int(os.getenv('WEB_PORT', '8080'))
        except ValueError:
            raise SettingsError('WEB_PORT 必须是整数。') from None
        settings = cls(os.getenv('WEB_HOST', '127.0.0.1'), port, root.resolve(),
                       os.getenv('WEB_OPEN_BROWSER', 'true').lower() in {'true', '1', 'yes'},
                       os.getenv('WEB_DEV', '').lower() in {'true', '1', 'yes'})
        settings.validate()
        return settings

    def travel_settings(self):
        return Settings('', '', frozenset(), os.getenv('AMAP_API_KEY', '').strip(),
                        os.getenv('LLM_API_KEY', '').strip(), os.getenv('LLM_BASE_URL', '').strip(),
                        os.getenv('LLM_MODEL_ID', '').strip())
