"""Raw WeChat routing, source admission and checkpoint serialization policy."""
import json
import re
import uuid
from ...core.contracts import Conversation, ConversationKind, Receipt
from ...forward_wire import WireError
from ...group_trigger import admitted, group_id, triggered
from .messages import text, native_message, valid_content, ImageMessage, MAX_TEXT


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

    def validate_message(self, message, image_input):
        if (message.account != self.account or message.direction != 'incoming' or message.origin != 'live'
                or not valid_content(message, image_input) or not text(message.id, 512)):
            return False
        if message.room_id == self.peer:
            return message.sender_id == self.peer and not self.is_self(message)
        return (self.destination(message.room_id) and group_id(message.room_id)
                and text(message.sender_id, 256) and not message.sender_id.endswith('@chatroom') and not self.is_self(message)
                and (self.all_groups or text(message.body, MAX_TEXT) and admitted(message.body,
                    getattr(message, 'mentioned', False), self.wake_mode, self.prefix,
                    context=self.group_context, image=isinstance(message, ImageMessage))))

    def wakeup(self, message):
        return (getattr(message, 'mentioned', False) if self.all_groups else
                triggered(message.body, getattr(message, 'mentioned', False), self.wake_mode, self.prefix))

    def body(self, message):
        return (message.body[len(self.prefix):] if not self.all_groups and self.wakeup(message)
                and message.body.startswith(self.prefix) else message.body)

    @staticmethod
    def accepted(receipt, account, destination):
        try:
            identity = uuid.UUID(receipt.send_id)
            return (isinstance(receipt, Receipt) and receipt.account == account
                    and receipt.destination == destination and receipt.state == 'accepted'
                    and identity.version == 4 and str(identity) == receipt.send_id
                    and isinstance(receipt.client_id, str) and re.fullmatch(r'[1-9][0-9]{0,9}', receipt.client_id)
                    and int(receipt.client_id) <= 2**32-1
                    and isinstance(receipt.server_id, str) and re.fullmatch(r'[1-9][0-9]{0,19}', receipt.server_id)
                    and int(receipt.server_id) <= 2**64-1)
        except (ValueError, AttributeError, TypeError):
            return False
