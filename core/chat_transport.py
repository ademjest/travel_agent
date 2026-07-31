from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


def storage_scope_id(platform: str, scope_id: str) -> str:
    if platform == "qq_official":
        return scope_id
    return f"{platform}:{scope_id}"


@dataclass(frozen=True)
class OutgoingMessage:
    channel: Literal["group", "private"]
    target_id: str
    reply_to_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ChatAttachment:
    filename: str
    url: str
    content_type: str = ""
    size: int = 0


@dataclass(frozen=True)
class ChatEvent:
    platform: Literal["qq_official", "onebot"]
    channel: Literal["group", "private"]
    event_id: str
    scope_id: str
    sender_id: str
    content: str
    reply_to_id: str = ""
    attachments: tuple[ChatAttachment, ...] = ()

    @property
    def event_key(self) -> str:
        return f"{self.platform}:{self.channel}:{self.scope_id}:{self.event_id}"

    @property
    def storage_scope_id(self) -> str:
        return storage_scope_id(self.platform, self.scope_id)


class MessageTransport(Protocol):
    async def send(self, message: OutgoingMessage) -> None:
        raise NotImplementedError


class ReplyRenderer(Protocol):
    def render(
            self,
            channel: Literal["group", "private"],
            command_content: str,
            reply_text: str) -> dict[str, Any]:
        raise NotImplementedError
