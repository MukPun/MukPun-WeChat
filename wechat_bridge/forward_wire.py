"""Bounded production IPC; only anonymous pipes carry message payloads."""
import json
import re

MAX_FRAME = 262144
HEX = re.compile(r'[0-9a-f]{32}\Z')


class WireError(Exception):
    pass


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_FRAME:
        raise WireError('frame_size')
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise WireError('duplicate_key')
            value[key] = item
        return value
    def invalid(_):
        raise WireError('constant')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise WireError('frame_type')
    return value


def encode(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(raw) > MAX_FRAME:
        raise WireError('frame_size')
    return raw


class Lines:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, raw):
        if len(self.buffer) + len(raw) > MAX_FRAME * 2:
            raise WireError('backpressure')
        self.buffer.extend(raw)

    def pop(self):
        if b'\n' not in self.buffer:
            if len(self.buffer) >= MAX_FRAME:
                raise WireError('frame_size')
            return None
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer[:] = rest
        return decode(bytes(line))
