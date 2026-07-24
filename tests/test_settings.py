import os
import unittest
from unittest.mock import patch

from settings import Settings


class SettingsTests(unittest.TestCase):
    def test_empty_official_allowlist_fails_closed(self):
        with patch.dict(os.environ, {
            "QQ_BOT_APPID": "appid",
            "QQ_BOT_SECRET": "secret",
            "QQ_BOT_ALLOWED_GROUPS": "",
            "QQ_BOT_ALLOW_ALL_GROUPS": "",
        }, clear=True):
            settings = Settings.from_env()

        self.assertFalse(settings.allows_group("group-a"))

    def test_explicit_development_override_allows_groups(self):
        with patch.dict(os.environ, {
            "QQ_BOT_APPID": "appid",
            "QQ_BOT_SECRET": "secret",
            "QQ_BOT_ALLOWED_GROUPS": "",
            "QQ_BOT_ALLOW_ALL_GROUPS": "true",
        }, clear=True):
            settings = Settings.from_env()

        self.assertTrue(settings.allows_group("group-a"))


if __name__ == "__main__":
    unittest.main()
