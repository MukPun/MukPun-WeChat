"""Identity-only loop protection. Never deduplicate messages by their text."""
import hashlib
import re


REASONS = frozenset({'own_sender', 'outgoing_event', 'outgoing_message', 'duplicate_input', 'duplicate_action'})
HEX = re.compile(r'[0-9a-f]{64}\Z')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')


def sender_id(value):
    return (isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 256
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and not value.endswith('@chatroom'))


def proof(value):
    if (not isinstance(value, dict) or set(value) != {'sendId', 'destinationSha256', 'textSha256', 'bytes'}
            or not isinstance(value['sendId'], str) or not UUID.fullmatch(value['sendId'])
            or any(not isinstance(value[k], str) or not HEX.fullmatch(value[k]) for k in ('destinationSha256', 'textSha256'))
            or type(value['bytes']) is not int or not 0 < value['bytes'] <= 8192):
        raise ValueError('self_proof_invalid')
    return value


def verified_sender(seed, intent, message, account, destinations):
    """Bind our sender to a previously accepted, exactly verified text send."""
    proof(seed)
    if (not isinstance(intent, dict) or not isinstance(message, dict)
            or intent.get('id') != seed['sendId'] or intent.get('accountId') != account
            or intent.get('state') != 'accepted' or intent.get('destination') not in destinations
            or not isinstance(intent.get('localMessageId'), str) or not intent['localMessageId']
            or message.get('id') != intent['localMessageId'] or message.get('accountId') != account
            or message.get('threadId') != intent['destination'] or message.get('direction') != 'outgoing'
            or type(message.get('type')) is not int or message['type'] != 1
            or not sender_id(message.get('senderId')) or message['senderId'] in destinations
            or not isinstance(message.get('body'), str)
            or hashlib.sha256(intent['destination'].encode()).hexdigest() != seed['destinationSha256']
            or len(message['body'].encode()) != seed['bytes']
            or hashlib.sha256(message['body'].encode()).hexdigest() != seed['textSha256']):
        raise ValueError('self_identity_unverified')
    return message['senderId']
