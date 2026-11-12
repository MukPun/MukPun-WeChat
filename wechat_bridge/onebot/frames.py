"""Protocol-owned bounded request parsing and determinate parse responses."""
from .codec import DecodeError, decode

MAX_ACTION_BYTES = 16 * 1024 * 1024


def decode_action(raw):
    return decode(raw, maximum=MAX_ACTION_BYTES)


async def handle_frame(service, raw):
    try:
        request = decode_action(raw)
    except DecodeError as error:
        answer = service.result(1400, message='invalid_json')
        if error.has_echo:
            answer['echo'] = error.echo
    except (ValueError, TypeError, UnicodeError, RecursionError):
        answer = service.result(1400, message='invalid_json')
    else:
        raw = None
        return request, await service.action(request), False
    return None, answer, True


def reply_sent(request, answer, callback):
    """Report only parsed send actions after the transport confirms its write."""
    if isinstance(request, dict) and request.get('action') in {'send_group_msg', 'send_private_msg', 'send_msg'}:
        callback(answer)
