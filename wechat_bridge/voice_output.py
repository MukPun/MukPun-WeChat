"""Inline PCM WAV to the pinned Native's mono PCM16/16kHz fast path."""
import base64
import hashlib
import re
import struct
import warnings

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import audioop  # Runtime is pinned to Python 3.12.

try:
    from .image_output import receipt_bound, accepted, job_id
except ImportError:
    # Native-side bundles flatten public modules; no package/sys.path access.
    import importlib.util
    from pathlib import Path
    import sys
    _spec = importlib.util.spec_from_file_location('_voice_receipt_contract', Path(__file__).with_name('image_output.py'))
    _contract = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _contract
    _spec.loader.exec_module(_contract)
    receipt_bound, accepted, job_id = _contract.receipt_bound, _contract.accepted, _contract.job_id

MAX_SOURCE = 8 * 1024 * 1024
MAX_SECONDS = 300
MAX_AUDIO = MAX_SECONDS * 32000 + 44
CHUNK_BYTES = 65536
META = {'mime', 'bytes', 'sha256', 'duration_ms'}
ERRORS = frozenset('output_voice_' + name for name in
    ('segment', 'source', 'size', 'base64', 'format', 'duration', 'payload', 'integrity', 'chunk', 'metadata'))


class OutputError(ValueError):
    pass


def _pcm(raw):
    if (not isinstance(raw, bytes) or len(raw) < 44 or raw[:4] != b'RIFF'
            or raw[8:12] != b'WAVE' or struct.unpack_from('<I', raw, 4)[0] + 8 != len(raw)):
        raise OutputError('output_voice_format')
    at, count, fmt, data = 12, 0, None, None
    while at < len(raw):
        count += 1
        if count > 1024 or at + 8 > len(raw):
            raise OutputError('output_voice_format')
        kind, length = raw[at:at + 4], struct.unpack_from('<I', raw, at + 4)[0]
        at += 8
        end = at + length
        if end + (length & 1) > len(raw):
            raise OutputError('output_voice_format')
        if kind == b'fmt ':
            if fmt is not None or length not in (16, 18) or length == 18 and raw[at + 16:end] != b'\0\0':
                raise OutputError('output_voice_format')
            fmt = struct.unpack_from('<HHIIHH', raw, at)
        elif kind == b'data':
            if data is not None:
                raise OutputError('output_voice_format')
            data = memoryview(raw)[at:end]
        at = end + (length & 1)
    if fmt is None or data is None:
        raise OutputError('output_voice_format')
    encoding, channels, rate, byte_rate, block, bits = fmt
    width = bits // 8
    if (encoding != 1 or channels not in (1, 2) or bits not in (8, 16, 24, 32)
            or not 8000 <= rate <= 96000 or block != channels * width
            or byte_rate != rate * block or len(data) % block):
        raise OutputError('output_voice_format')
    if not data or len(data) > MAX_SECONDS * rate * block:
        raise OutputError('output_voice_duration')
    return data, channels, rate, width


def _wav(pcm):
    return (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVEfmt '
            + struct.pack('<IHHIIHH', 16, 1, 1, 16000, 32000, 2, 16)
            + b'data' + struct.pack('<I', len(pcm)) + pcm)


def payload(raw):
    data, channels, rate, width = _pcm(raw)
    if (channels, rate, width) != (1, 16000, 2):
        raise OutputError('output_voice_format')
    if len(raw) != 44 + len(data) or raw[12:16] != b'fmt ' or raw[36:40] != b'data':
        raise OutputError('output_voice_format')
    duration = len(data) * 1000 // 32000
    if not 1 <= duration <= MAX_SECONDS * 1000:
        raise OutputError('output_voice_duration')
    return {'mime': 'audio/wav', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'duration_ms': duration, 'base64': base64.b64encode(raw).decode('ascii')}


def normalize(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SOURCE:
        raise OutputError('output_voice_size')
    data, channels, rate, width = _pcm(raw)
    output, state = bytearray(), None
    # Process aligned small blocks to keep conversion scratch bounded.
    block = 16384 * channels * width
    for offset in range(0, len(data), block):
        part = bytes(data[offset:offset + block])
        if width == 1:
            part = audioop.bias(part, 1, -128)
        if channels == 2:
            part = audioop.tomono(part, width, .5, .5)
        if width != 2:
            part = audioop.lin2lin(part, width, 2)
        if rate != 16000:
            part, state = audioop.ratecv(part, 2, 1, rate, 16000, state)
        output.extend(part)
        if len(output) > MAX_AUDIO - 44:
            raise OutputError('output_voice_duration')
    expected = (len(data) // (channels * width) * 16000 + rate // 2) // rate * 2
    if len(output) < expected:
        output.extend((output[-2:] or b'\0\0') * ((expected - len(output)) // 2))
    return payload(_wav(bytes(output[:expected])))


def from_segment(segment):
    if (not isinstance(segment, dict) or set(segment) != {'type', 'data'} or segment.get('type') != 'record'
            or not isinstance(segment.get('data'), dict) or set(segment['data']) != {'file'}):
        raise OutputError('output_voice_segment')
    source = segment['data']['file']
    if not isinstance(source, str) or not source.startswith('base64://') or len(source) == 9:
        raise OutputError('output_voice_source')
    if len(source) - 9 > 4 * ((MAX_SOURCE + 2) // 3):
        raise OutputError('output_voice_size')
    try:
        raw = base64.b64decode(source[9:], validate=True)
    except (ValueError, UnicodeError):
        raise OutputError('output_voice_base64') from None
    return normalize(raw)


def metadata(value):
    return {key: value[key] for key in META}


def _metadata(meta):
    if (not isinstance(meta, dict) or set(meta) != META or meta.get('mime') != 'audio/wav'
            or type(meta.get('bytes')) is not int or not 76 <= meta['bytes'] <= MAX_AUDIO
            or (meta['bytes'] - 44) % 2 or type(meta.get('duration_ms')) is not int
            or meta['duration_ms'] != (meta['bytes'] - 44) * 1000 // 32000
            or not isinstance(meta.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', meta['sha256'])):
        raise OutputError('output_voice_metadata')


def validate(value):
    if not isinstance(value, dict) or set(value) != META | {'base64'}:
        raise OutputError('output_voice_payload')
    _metadata(metadata(value))
    if not isinstance(value['base64'], str) or len(value['base64']) != 4 * ((value['bytes'] + 2) // 3):
        raise OutputError('output_voice_payload')
    try:
        raw = base64.b64decode(value['base64'], validate=True)
    except ValueError:
        raise OutputError('output_voice_base64') from None
    if payload(raw) != value:
        raise OutputError('output_voice_integrity')
    return value


class Assembly:
    def __init__(self, meta):
        _metadata(meta)
        self.meta, self.raw, self.next = dict(meta), bytearray(), 0
    def add(self, index, encoded):
        if (type(index) is not int or index != self.next or not isinstance(encoded, str)
                or not 0 < len(encoded) <= 4 * ((CHUNK_BYTES + 2) // 3)):
            raise OutputError('output_voice_chunk')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise OutputError('output_voice_chunk') from None
        expected = min(CHUNK_BYTES, self.meta['bytes'] - len(self.raw))
        if expected <= 0 or len(raw) != expected or base64.b64encode(raw).decode('ascii') != encoded:
            raise OutputError('output_voice_chunk')
        self.raw.extend(raw); self.next += 1
    def check(self):
        if len(self.raw) != self.meta['bytes'] or hashlib.sha256(self.raw).hexdigest() != self.meta['sha256']:
            raise OutputError('output_voice_integrity')
        raw = bytes(self.raw)
        data, channels, rate, width = _pcm(raw)
        if ((channels, rate, width) != (1, 16000, 2) or len(raw) != 44 + len(data)
                or raw[12:16] != b'fmt ' or raw[36:40] != b'data'
                or len(data) * 1000 // 32000 != self.meta['duration_ms']):
            raise OutputError('output_voice_integrity')

    def finish(self):
        self.check()
        result = payload(bytes(self.raw))
        if metadata(result) != self.meta:
            raise OutputError('output_voice_integrity')
        return result
