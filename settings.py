import os
from dataclasses import dataclass


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    appid: str
    secret: str
    allowed_group_openids: frozenset[str]
    amap_api_key: str
    llm_api_key: str
    llm_base_url: str
    llm_model_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        appid = os.getenv("QQ_BOT_APPID", "").strip()
        secret = os.getenv("QQ_BOT_SECRET", "").strip()
        amap_api_key = os.getenv("AMAP_API_KEY", "").strip()
        llm_api_key = os.getenv("LLM_API_KEY", "").strip()
        llm_base_url = os.getenv("LLM_BASE_URL", "").strip()
        llm_model_id = os.getenv("LLM_MODEL_ID", "").strip()
        allowed_groups = frozenset(
            value.strip()
            for value in os.getenv("QQ_BOT_ALLOWED_GROUPS", "").split(",")
            if value.strip()
        )

        missing = []
        if not appid:
            missing.append("QQ_BOT_APPID")
        if not secret:
            missing.append("QQ_BOT_SECRET")
        if missing:
            names = ", ".join(missing)
            raise SettingsError(f"Missing required environment variables: {names}")

        return cls(
            appid=appid,
            secret=secret,
            allowed_group_openids=allowed_groups,
            amap_api_key=amap_api_key,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model_id=llm_model_id,
        )

    def allows_group(self, group_openid: str) -> bool:
        return (
            not self.allowed_group_openids
            or group_openid in self.allowed_group_openids
        )

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.llm_api_key
            and self.llm_base_url
            and self.llm_model_id
        )


@dataclass(frozen=True)
class OneBotSettings:
    http_url: str
    access_token: str
    inbound_token: str
    allowed_group_ids: frozenset[str]
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    @classmethod
    def from_env(cls) -> "OneBotSettings":
        access_token = os.getenv("ONEBOT_ACCESS_TOKEN", "").strip()
        inbound_token = os.getenv("ONEBOT_INBOUND_TOKEN", "").strip()
        missing = []
        if not access_token:
            missing.append("ONEBOT_ACCESS_TOKEN")
        if not inbound_token:
            missing.append("ONEBOT_INBOUND_TOKEN")
        if missing:
            raise SettingsError(
                "Missing required environment variables: "
                + ", ".join(missing)
            )

        raw_port = os.getenv("ONEBOT_BIND_PORT", "8000").strip()
        try:
            bind_port = int(raw_port)
        except ValueError as exc:
            raise SettingsError("ONEBOT_BIND_PORT must be an integer") from exc
        if not 1 <= bind_port <= 65535:
            raise SettingsError("ONEBOT_BIND_PORT must be between 1 and 65535")

        return cls(
            http_url=(
                os.getenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
                .strip()
                .rstrip("/")
            ),
            access_token=access_token,
            inbound_token=inbound_token,
            allowed_group_ids=frozenset(
                value.strip()
                for value in os.getenv("ONEBOT_ALLOWED_GROUPS", "").split(",")
                if value.strip()
            ),
            bind_host=(
                os.getenv("ONEBOT_BIND_HOST", "127.0.0.1").strip()
                or "127.0.0.1"
            ),
            bind_port=bind_port,
        )

    def allows_group(self, group_id: str) -> bool:
        return str(group_id) in self.allowed_group_ids
