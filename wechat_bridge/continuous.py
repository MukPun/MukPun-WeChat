"""Compatibility constructors/exports for the composed continuous OneBot service."""
import time
from .adapters.wechat.assembly import service_options
from .onebot.service import OneBotService
from .onebot.codec import UnsupportedMessageSegment, number
from .core.contracts import NativeMessage, Receipt
from .adapters.wechat.legacy import Bridge
from .adapters.wechat.messages import (
    MAX_TEXT, RoutedMessage, ImageMessage, native_message, valid_content, text,
)
from .core.text import split_text


class PrivateBridge(OneBotService):
    def __init__(self, config, peer, state, backend, *, clock=time.time, segment_bytes=8192,
                self_sender_id=None, image_input=False, image_output=False, voice_output=False):
        OneBotService.__init__(self, config, peer, state, backend, **service_options(config, peer, clock=clock, segment_bytes=segment_bytes,
            self_sender_id=self_sender_id, image_input=image_input,
            image_output=image_output, voice_output=voice_output))


class TextBridge(PrivateBridge):
    def __init__(self, config, peer, state, backend, *, clock=time.time, prefix='/ai ', segment_bytes=8192,
                private_name='测试联系人', wake_mode='prefix', self_sender_id=None, image_input=False,
                group_context=False, image_output=False, all_groups=False, voice_output=False):
        OneBotService.__init__(self, config, peer, state, backend, **service_options(config, peer, clock=clock, groups=True,
            prefix=prefix, segment_bytes=segment_bytes, private_name=private_name, wake_mode=wake_mode,
            self_sender_id=self_sender_id, image_input=image_input, group_context=group_context,
            image_output=image_output, all_groups=all_groups, voice_output=voice_output))
