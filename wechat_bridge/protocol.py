"""Compatibility exports for the original protocol import surface."""
from .core.contracts import NativeMessage, Receipt
from .storage import StateError
from .core.text import text
from .onebot.codec import (MAX_JSON, MAX_TEXT, MAX_JSON_DEPTH, MAX_JSON_TOKENS,
                          DecodeError, UnsupportedMessageSegment, number, decode)
from .adapters.wechat.config import Config
from .adapters.wechat.legacy import Bridge
