"""Compatibility exports for the composed reverse WebSocket transport."""
import asyncio
import time
from websockets.asyncio.client import connect
from .onebot.codec import MAX_JSON, DecodeError, decode
from .onebot.frames import decode_action
from .transports.config import Endpoint
from .transports.reverse_ws import MAX_ACTION_BYTES, Runner as ReverseWebSocketRunner


class Runner(ReverseWebSocketRunner):
    def __init__(self, bridge, endpoint, *, on_reply=None, event_source=None,
                 connector=None, clock=time.monotonic):
        super().__init__(bridge, endpoint, on_reply=on_reply, event_source=event_source,
                         connector=connector or (lambda *a, **kw: connect(*a, **kw)), clock=clock)
