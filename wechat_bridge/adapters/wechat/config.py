"""Legacy public configuration validation, including WeChat room syntax."""
from dataclasses import dataclass, field
import math
from ...core.text import text

@dataclass(frozen=True)
class Config:
    account: str = field(repr=False)
    self_native_id: str = field(repr=False)
    room_id: str | None = field(repr=False)
    room_name: str
    reply_seconds: int = 90
    send_interval: float = 5.

    def __post_init__(self):
        if (not text(self.account, 64) or not text(self.self_native_id, 256)
                or self.room_id is not None and (not text(self.room_id, 256)
                    or self.room_id == '@chatroom' or not self.room_id.endswith('@chatroom'))
                or not text(self.room_name, 256) or type(self.reply_seconds) is not int
                or not 1 <= self.reply_seconds <= 180
                or type(self.send_interval) not in (int, float) or not math.isfinite(self.send_interval)
                or not 0 <= self.send_interval <= 60):
            raise ValueError('invalid_config')
