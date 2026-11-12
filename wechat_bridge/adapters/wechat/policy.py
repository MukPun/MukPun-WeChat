"""Raw WeChat routing, source admission and checkpoint serialization policy."""
import json
import re
from ...core.contracts import Conversation, ConversationKind
from ...forward_wire import WireError
from ...group_trigger import admitted, group_id
from .messages import text, native_message


class WeChatPolicy:
    project_message = staticmethod(native_message)

    def __init__(self, account, peer, *, group=None, prefix='/ai ', wake_mode='prefix',
                 self_sender_id=None, group_context=False, all_groups=False):
        self.account, self.peer, self.group = account, peer, group
        self.prefix, self.wake_mode = prefix, wake_mode
        self.self_sender_id = self_sender_id
        self.group_context, self.all_groups = group_context, all_groups
        self.destinations = {peer} | ({group} if group else set())

    def conversation(self, account, raw_id):
        return Conversation(account, raw_id, ConversationKind.GROUP if group_id(raw_id) else ConversationKind.PRIVATE)

    def is_self(self, message):
        return message.direction == 'outgoing' or message.sender_id == self.self_sender_id

    def destination(self, destination):
        return destination in self.destinations or self.all_groups and group_id(destination)

    def check_source(self, data):
        if (not isinstance(data, dict) or data.get('account') != self.account
                or not self.destination(data.get('room_id'))
                or data.get('direction') != 'incoming' or data.get('origin') != 'live'
                or not text(data.get('id'), 512) or type(data.get('timestamp')) is not int
                or not text(data.get('sender_id'), 256) or data['sender_id'].endswith('@chatroom')
                or data['sender_id'] == self.self_sender_id
                or data['room_id'] == self.peer and data['sender_id'] != self.peer
                or type(data.get('mentioned', False)) is not bool
                or not self.all_groups and data['room_id'] == self.group and not admitted(data.get('body'), data.get('mentioned', False),
                    self.wake_mode, self.prefix, context=self.group_context, image='image' in data)):
            raise WireError('source_binding')

    @staticmethod
    def checkpoint(value):
        cursor = value['cursor']
        if not isinstance(cursor, str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}', cursor) or int(cursor) > 2**63 - 1:
            raise WireError('cursor')
        anchor = value.get('anchor')
        if anchor is not None and (not isinstance(anchor, str) or not re.fullmatch('[0-9a-f]{64}', anchor)):
            raise WireError('anchor')
        return json.dumps({'cursor': cursor, 'anchor': anchor}, separators=(',', ':')) if anchor else cursor
