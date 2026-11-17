"""Bounded production IPC; only anonymous pipes carry message payloads."""
import json
import re

MAX_FRAME = 262144
HEX = re.compile(r'[0-9a-f]{32}\Z')


def recovery_protection(value):
    if (not isinstance(value, dict) or set(value) != {'floorSeq', 'epoch'}
            or type(value['floorSeq']) is not int or not 1 <= value['floorSeq'] <= 2**63-1
            or not isinstance(value['epoch'], str) or not HEX.fullmatch(value['epoch'])):
        raise WireError('recovery_protection')
    return value


class WireError(Exception):
    pass


def live_start(value):
    if (not isinstance(value, dict) or set(value) != {'runId', 'checkpoint'}
            or not isinstance(value['runId'], str) or not HEX.fullmatch(value['runId'])):
        raise WireError('live_receive_boundary')
    checkpoint = value['checkpoint']
    if (not isinstance(checkpoint, dict) or set(checkpoint) != {'cursor', 'anchor'}
            or not isinstance(checkpoint['cursor'], str) or not re.fullmatch('[0-9]{1,20}', checkpoint['cursor'])
            or not 0 < int(checkpoint['cursor']) < 2**63
            or not isinstance(checkpoint['anchor'], str) or not re.fullmatch('[0-9a-f]{64}', checkpoint['anchor'])):
        raise WireError('live_receive_boundary')
    return value


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


# This file is also loaded by importlib under -I and in Native's flat /opt.
# Keep one pure health implementation available in all three code layouts.
try:
    from .core import health as health_state
except ImportError:
    import importlib.util
    from pathlib import Path
    import sys
    _health_path = Path(__file__).with_name('health.py')
    if not _health_path.is_file():
        _health_path = Path(__file__).parent / 'core' / 'health.py'
    _health_spec = importlib.util.spec_from_file_location('_forward_health_contract', _health_path)
    health_state = importlib.util.module_from_spec(_health_spec)
    sys.modules[_health_spec.name] = health_state
    _health_spec.loader.exec_module(health_state)

health_frame = health_state.validate_health
PROTOCOL_VERSION = 2


def compatibility(digest):
    value = {'version': PROTOCOL_VERSION, 'digest': digest}
    verify_compatibility(value, digest)
    return value


def verify_compatibility(value, digest):
    if (not isinstance(value, dict) or set(value) != {'version', 'digest'}
            or type(value['version']) is not int or value['version'] != PROTOCOL_VERSION
            or not isinstance(value['digest'], str) or re.fullmatch('[0-9a-f]{64}', value['digest']) is None
            or value['digest'] != digest):
        raise ValueError('worker_compatibility')
    return value


def ready_frame(digest):
    compatibility(digest)
    return ('AWG-READY/' + str(PROTOCOL_VERSION) + ' ' + digest + '\n').encode('ascii')
