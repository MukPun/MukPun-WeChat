"""Bounded OneBot JSON decoding, identifiers and response envelopes."""
import json
import math
import re
from ..core.text import text

MAX_JSON = 65536
MAX_TEXT = 8192
MAX_JSON_DEPTH = 32

MAX_JSON_TOKENS = 8192

_NO_ECHO = object()

class DecodeError(ValueError):
    """Invalid action; an unambiguous, bounded echo may still be returned."""
    def __init__(self, code, *, echo=_NO_ECHO):
        super().__init__(code)
        self.has_echo = echo is not _NO_ECHO
        self.echo = echo if self.has_echo else None

class _JSONObject(dict):
    __slots__ = ('duplicates',)

    def __init__(self):
        super().__init__()
        self.duplicates = set()

def _structure_budget(raw):
    # Bound containers and members before json.loads allocates their objects.
    # Long opaque base64 strings do not consume this structural budget.
    quoted = escaped = False
    depth = tokens = 0
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        else:
            if char in '[{':
                depth += 1
                tokens += 1
                if depth > MAX_JSON_DEPTH:
                    raise DecodeError('json_structure_limit')
            elif char in ']}':
                depth -= 1
            elif char in ',:':
                tokens += 1
            if tokens > MAX_JSON_TOKENS:
                raise DecodeError('json_structure_limit')

def _unique_echo(value):
    if not isinstance(value, _JSONObject) or 'echo' not in value or 'echo' in value.duplicates:
        return _NO_ECHO
    echo = value['echo']
    pending = [echo]
    while pending:
        part = pending.pop()
        if isinstance(part, _JSONObject):
            if part.duplicates:
                return _NO_ECHO
            pending.extend(part.values())
        elif isinstance(part, list):
            pending.extend(part)
    try:
        return echo if len(json.dumps(echo, ensure_ascii=True, allow_nan=False)) <= 4096 else _NO_ECHO
    except (ValueError, TypeError, RecursionError):
        return _NO_ECHO

class UnsupportedMessageSegment(ValueError):
    """Rejected before any native submission; safe to keep forwarding."""

def number(value):
    if type(value) is int and 0 < value <= 2147483647:
        return value
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,9}', value) and int(value) <= 2147483647:
        return int(value)
    raise ValueError('invalid_id')

def decode(raw, *, maximum=MAX_JSON):
    if type(maximum) is not int or maximum <= 0 or not isinstance(raw, str) or len(raw.encode('utf-8')) > maximum:
        raise DecodeError('invalid_json')
    _structure_budget(raw)
    duplicates = False
    def pairs(items):
        nonlocal duplicates
        result = _JSONObject()
        for key, value in items:
            if key in result:
                result.duplicates.add(key)
                duplicates = True
            result[key] = value
        return result
    def invalid(_):
        raise DecodeError('invalid_constant')
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise DecodeError('invalid_constant')
        return number
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)
    if not isinstance(result, dict):
        raise DecodeError('invalid_json')
    if duplicates:
        raise DecodeError('duplicate_key', echo=_unique_echo(result))
    return result

def result(code=0, data=None, message=''):
    value = {'status': 'ok' if code == 0 else 'failed', 'retcode': code, 'data': data}
    if message:
        value['message'] = value['wording'] = message
    return value
