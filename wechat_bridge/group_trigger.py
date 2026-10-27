"""Group wakeup policy shared by the isolated reader and OneBot side."""
MODES = {'prefix', 'mention_or_prefix', 'mention'}


def group_id(value):
    """Recognize Native group destinations without a name or catalog whitelist."""
    return (isinstance(value, str) and value != '@chatroom' and value.endswith('@chatroom')
            and len(value.encode('utf-8')) <= 256
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def self_mentioned(value):
    if (not isinstance(value, dict) or value.get('state') != 'known'
            or value.get('source') != 'msgsource.atuserlist' or value.get('self') is not True):
        return False
    ids = value.get('userIds')
    return (isinstance(ids, list) and 0 < len(ids) <= 128 and all(
        isinstance(item, str) and 0 < len(item.encode('utf-8')) <= 256
        and not any(ord(c) < 32 or ord(c) == 127 for c in item) for item in ids))


def triggered(body, mentioned, mode='prefix', prefix='/ai '):
    if not isinstance(mode, str) or mode not in MODES or not isinstance(body, str) or type(mentioned) is not bool:
        return False
    by_prefix = body.startswith(prefix) and bool(body[len(prefix):].strip())
    return bool((mode in {'mention', 'mention_or_prefix'} and mentioned)
                or mode in {'prefix', 'mention_or_prefix'} and by_prefix)


def admitted(body, mentioned, mode='prefix', prefix='/ai ', *, context=False, image=False):
    """Context broadens plain text admission, never media or wake semantics."""
    if (type(context) is not bool or type(image) is not bool or type(mentioned) is not bool
            or not isinstance(mode, str) or mode not in MODES or not isinstance(body, str)):
        return False
    return triggered(body, mentioned, mode, prefix) or (context and not image and bool(body.strip()))
