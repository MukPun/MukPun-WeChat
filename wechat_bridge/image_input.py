"""Bounded image payloads; no URL fetching, model calls or image decoding."""
import base64
import binascii
import hashlib
import re
import struct

MAX_IMAGE = 128 * 1024
MAX_EDGE = 2048
MAX_PIXELS = 4 * 1024 * 1024
MIMES = frozenset({'image/jpeg', 'image/png', 'image/gif', 'image/webp'})
ERROR_CODES = frozenset({'image_capacity', 'image_dimensions', 'image_limits', 'image_payload', 'image_base64', 'image_integrity',
    'image_account', 'image_timeout', 'image_response_size', 'image_native_rejected', 'image_content_type',
    'image_native_shape', 'image_transport', 'image_identity', 'image_assets', 'image_unavailable',
    'image_job_identity', 'image_job_binding', 'image_download_failed', 'image_download_size',
    'image_asset_identity', 'image_asset_binding', 'image_asset_integrity', 'image_probe_shape', 'image_failed', 'image_quote_binding',
    'sticker_limits', 'sticker_unavailable', 'sticker_source_not_allowed', 'unsupported_sticker_source', 'media_metadata_invalid',
    'media_metadata_unavailable', 'unsupported_media_transport'})


class ImageError(ValueError):
    pass


def dimensions(raw, mime):
    width = height = 0
    if mime == 'image/png' and raw[:8] == b'\x89PNG\r\n\x1a\n' and raw[12:16] == b'IHDR' and len(raw) >= 24:
        width, height = struct.unpack('>II', raw[16:24])
    elif mime == 'image/gif' and raw[:6] in (b'GIF87a', b'GIF89a') and len(raw) >= 10:
        width, height = struct.unpack('<HH', raw[6:10])
    elif mime == 'image/jpeg' and raw[:2] == b'\xff\xd8':
        offset = 2
        while offset + 4 <= len(raw):
            if raw[offset] != 255:
                break
            marker = raw[offset + 1]
            offset += 2
            if marker == 255:
                offset -= 1
                continue
            if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
                continue
            size = int.from_bytes(raw[offset:offset + 2], 'big')
            if size < 2 or offset + size > len(raw):
                break
            if marker in {0xc0, 0xc1, 0xc2} and size >= 8:
                height, width = struct.unpack('>HH', raw[offset + 3:offset + 7])
                break
            if marker == 0xda:
                break
            offset += size
    elif mime == 'image/webp' and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP' and len(raw) >= 30:
        kind = raw[12:16]
        if kind == b'VP8X':
            width = 1 + int.from_bytes(raw[24:27], 'little')
            height = 1 + int.from_bytes(raw[27:30], 'little')
        elif kind == b'VP8 ' and raw[23:26] == b'\x9d\x01\x2a':
            width, height = (value & 0x3fff for value in struct.unpack('<HH', raw[26:30]))
        elif kind == b'VP8L' and raw[20] == 0x2f:
            bits = int.from_bytes(raw[21:25], 'little')
            width, height = (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
    if not (0 < width <= MAX_EDGE and 0 < height <= MAX_EDGE and width * height <= MAX_PIXELS):
        raise ImageError('image_dimensions')
    return width, height


def payload(raw, mime, quality):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_IMAGE or mime not in MIMES or quality not in {'standard', 'thumbnail', 'original'}:
        raise ImageError('image_limits')
    dimensions(raw, mime)
    return {'mime': mime, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'quality': quality, 'base64': base64.b64encode(raw).decode('ascii')}


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'mime', 'bytes', 'sha256', 'quality', 'base64'}
            or type(value['bytes']) is not int or not 0 < value['bytes'] <= MAX_IMAGE
            or not isinstance(value['base64'], str) or len(value['base64']) != 4 * ((value['bytes'] + 2) // 3)
            or not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
        raise ImageError('image_payload')
    try:
        raw = base64.b64decode(value['base64'], validate=True)
    except (binascii.Error, ValueError):
        raise ImageError('image_base64') from None
    checked = payload(raw, value['mime'], value['quality'])
    if checked != value:
        raise ImageError('image_integrity')
    return value


def segment(value):
    validate(value)
    return {'type': 'image', 'data': {'file': 'base64://' + value['base64']}}
