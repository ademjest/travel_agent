from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class OutgoingMessage:
    channel: Literal["group", "private"]
    target_id: str
    reply_to_id: str
    payload: dict[str, Any]


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
