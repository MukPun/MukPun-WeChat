"""Small contracts; raw identities/checkpoints and platform proof remain opaque.

NativeMessage and Receipt retain their legacy fields for IPC/state compatibility.
The legacy protocol module re-exports these exact classes. Media dictionaries keep
existing bounded wire representations; adapters own validation and send evidence.
"""
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ConversationKind(StrEnum):
    GROUP = 'group'
    PRIVATE = 'private'


@dataclass(frozen=True)
class Conversation:
    account: str = field(repr=False)
    raw_id: str = field(repr=False)
    kind: ConversationKind


@dataclass(frozen=True)
class NativeMessage:
    account: str = field(repr=False)
    id: str = field(repr=False)
    room_id: str = field(repr=False)
    sender_id: str = field(repr=False)
    body: str = field(repr=False)
    timestamp: int
    origin: str = 'live'
    direction: str = 'incoming'
    type: int = 1


@dataclass(frozen=True)
class Receipt:
    account: str = field(repr=False)
    destination: str = field(repr=False)
    send_id: str = field(repr=False)
    state: str
    client_id: str = field(default='', repr=False)
    server_id: str = field(default='', repr=False)


class SendOutcome(StrEnum):
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    receipt: Receipt | None = field(default=None, repr=False)
    code: str = ''
    parts: tuple[Receipt, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Capabilities:
    text: bool = True
    image: bool = False
    voice: bool = False


@dataclass(frozen=True)
class Health:
    online: bool = False
    good: bool = False
    observed_at: float | None = None


class PlatformBackend(Protocol):
    """Existing async gateway surface; concrete adapters retain their evidence rules."""
    online: bool

    async def next_message(self) -> NativeMessage: ...
    async def send_text(self, destination: str, body: str, key: str) -> Receipt: ...
    async def send_image(self, destination: str, image: dict, key: str) -> Receipt: ...
    async def send_voice(self, destination: str, audio: dict, key: str) -> Receipt: ...


class PlatformPolicy(Protocol):
    """Resolve platform identity rules at the adapter boundary, never by ID suffix in core."""
    def conversation(self, account: str, raw_id: str) -> Conversation: ...
    def is_self(self, message: NativeMessage) -> bool: ...
