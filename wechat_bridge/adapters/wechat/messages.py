"""Existing normalized message DTOs and bounded content projection."""
from dataclasses import dataclass, field
from ...core.contracts import NativeMessage
from ...core.text import text as base_text
from ... import image_input as images

MAX_TEXT = 32768


@dataclass(frozen=True)
class RoutedMessage(NativeMessage):
    mentioned: bool = False
    sender_nickname: str = field(default='', repr=False, kw_only=True)
    sender_card: str | None = field(default=None, repr=False, kw_only=True)


@dataclass(frozen=True)
class ImageMessage(RoutedMessage):
    image: dict = field(default_factory=dict, repr=False)


def native_message(data):
    if 'image' in data:
        return ImageMessage(**data)
    return RoutedMessage(**data) if {'mentioned', 'sender_nickname', 'sender_card'} & data.keys() else NativeMessage(**data)


def valid_content(message, enabled):
    image = getattr(message, 'image', None)
    if image is not None:
        if not enabled:
            return False
        images.validate(image, allow_file=True)
    return (type(message.type) is int and
            (message.type == 1 and text(message.body, MAX_TEXT)
             or message.type == 3 and image is not None and message.body == ''))


def text(value, maximum=MAX_TEXT):
    # Preserve Windows newlines exactly; the original one-shot policy did not.
    return isinstance(value, str) and base_text(value.replace('\r', '\n'), maximum)
