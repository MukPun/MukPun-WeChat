"""OneBot event/name projection; native identities stay in persistent storage."""
from .. import image_input as images


def meta(service, subtype):
    base = {'time': int(service.clock()), 'self_id': service.self_id,
            'post_type': 'meta_event', 'meta_event_type': subtype}
    if subtype == 'lifecycle':
        return {**base, 'sub_type': 'connect'}
    status = service.status() if hasattr(service, 'status') else {'online': service.online, 'good': service.online}
    return {**base, 'status': status, 'interval': 15000}


class EventCodec:
    def __init__(self, private_name, room_name):
        self.private_name, self.room_name = private_name, room_name

    def group_name(self, messages, group):
        return self.room_name if group == messages.group_id else '微信群' + str(group)

    @staticmethod
    def sender(messages, group, user, message=None):
        if user == messages.self_id:
            nickname, card = '微信助手', ''
        else:
            nickname, card = messages.state.member_name(group, user,
                getattr(message, 'sender_nickname', ''), getattr(message, 'sender_card', None))
        return {'user_id': user, 'nickname': nickname or '微信成员' + str(user), 'card': card, 'role': 'member'}

    def message(self, messages, message, mid, user, group, wakeup):
        private = group is None
        body = message.body if private else messages.policy.body(message)
        segments = ([{'type': 'at', 'data': {'qq': str(messages.self_id)}}] if wakeup else [])
        if body or not private:
            segments.append({'type': 'text', 'data': {'text': body}})
        image = getattr(message, 'image', None)
        if image is not None:
            segments.append(images.segment(image))
        event = {'time': message.timestamp, 'self_id': messages.self_id, 'post_type': 'message',
                 'message_type': 'private' if private else 'group', 'sub_type': 'friend' if private else 'normal',
                 'message_id': mid, 'user_id': user, 'font': 0, 'raw_message': message.body,
                 'message': segments}
        if private:
            event['sender'] = {'user_id': user, 'nickname': self.private_name, 'sex': 'unknown', 'age': 0}
        else:
            event.update(group_id=group, group_name=self.group_name(messages, group), anonymous=None,
                         sender=self.sender(messages, group, user, message))
        return event


def legacy_message(service, message, mid, uid, body):
    """Preserve the one-shot trigger projection without changing its reply policy."""
    segments = [{'type': 'at', 'data': {'qq': str(service.self_id)}}, {'type': 'text', 'data': {'text': body}}]
    event = {'time': message.timestamp, 'self_id': service.self_id, 'post_type': 'message',
             'message_type': 'group', 'sub_type': 'normal', 'message_id': mid, 'group_id': service.group_id,
             'group_name': service.config.room_name, 'user_id': uid, 'message': segments,
             'raw_message': '/ai ' + body, 'font': 0, 'anonymous': None,
             'sender': service._sender(service.group_id, uid, message)}
    return event
