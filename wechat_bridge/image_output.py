"""Small static image output contract; never opens paths or fetches URLs."""
import base64
import binascii
import hashlib
import re
import struct
import uuid
import zlib

MAX_IMAGE = 8 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
# One strictly ordered round trip per chunk means the transfer deadline has to
# grow with the payload. A fixed short limit expired mid-assembly and left the
# audit claiming a send that had not started yet.
CHUNK_SECONDS = 2.0
ASSEMBLY_BASE_SECONDS = 20.0
MAX_ASSEMBLY_SECONDS = 300.0
SUBMIT_SECONDS = 85.0
TRANSFER_GRACE_SECONDS = 15.0
MAX_EDGE = 2048
MAX_PIXELS = 4 * 1024 * 1024
MIMES = frozenset({'image/jpeg', 'image/png', 'image/webp'})
RECEIPT_KEYS = frozenset({'account', 'destination', 'job_id', 'state', 'server_id',
                          'source_sha256', 'projection_verified'})
REJECTION_KEYS = frozenset({'reason', 'format', 'source_bytes', 'source_sha256', 'width', 'height'})
REJECTION_REASONS = frozenset({'output_disabled', 'segment', 'source', 'base64', 'format', 'size',
                               'dimensions', 'interlace', 'payload', 'integrity', 'capacity'})
REJECTION_FORMATS = frozenset({'png', 'jpeg', 'webp', 'gif', 'unknown'})
MAX_DIAGNOSTIC_IMAGE = 9 * 1024 * 1024
MAX_DIAGNOSTIC_HEADER = 64 * 1024


class OutputError(ValueError):
    pass


def validate_rejection(value):
    if (not isinstance(value, dict) or set(value) != REJECTION_KEYS
            or not isinstance(value.get('reason'), str) or value['reason'] not in REJECTION_REASONS
            or not isinstance(value.get('format'), str) or value['format'] not in REJECTION_FORMATS
            or type(value.get('source_bytes')) is not int or not 0 <= value['source_bytes'] <= MAX_DIAGNOSTIC_IMAGE
            or not isinstance(value.get('source_sha256'), str)
            or value['source_sha256'] and not re.fullmatch('[0-9a-f]{64}', value['source_sha256'])
            or any(type(value.get(key)) is not int or not 0 <= value[key] <= 65535 for key in ('width', 'height'))):
        raise OutputError('output_image_rejection')
    return value


def _rejection_header(raw):
    """Read untrusted format headers only; these dimensions do not validate an image."""
    header = raw[:MAX_DIAGNOSTIC_HEADER]
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        shape = struct.unpack('>II', header[16:24]) if len(header) >= 24 and header[12:16] == b'IHDR' else (0, 0)
        return 'png', shape
    if header[:6] in {b'GIF87a', b'GIF89a'}:
        return 'gif', struct.unpack('<HH', header[6:10]) if len(header) >= 10 else (0, 0)
    if header.startswith(b'\xff\xd8'):
        offset = 2
        while offset + 4 <= len(header):
            if header[offset] != 255:
                break
            marker = header[offset + 1]
            if marker == 255:
                offset += 1
                continue
            if marker in {0xda, 0xd9}:
                break
            length = int.from_bytes(header[offset + 2:offset + 4], 'big')
            if length < 2 or offset + 2 + length > len(header):
                break
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf} and length >= 8:
                height, width = struct.unpack('>HH', header[offset + 5:offset + 9])
                return 'jpeg', (width, height)
            offset += length + 2
        return 'jpeg', (0, 0)
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        offset = 12
        while offset + 8 <= len(header):
            kind = header[offset:offset + 4]
            length = int.from_bytes(header[offset + 4:offset + 8], 'little')
            body = header[offset + 8:offset + 18]
            if kind == b'VP8X' and length >= 10 and len(body) == 10:
                return 'webp', (int.from_bytes(body[4:7], 'little') + 1,
                                int.from_bytes(body[7:10], 'little') + 1)
            if kind == b'VP8 ' and length >= 10 and len(body) == 10 and body[3:6] == b'\x9d\x01\x2a':
                return 'webp', (int.from_bytes(body[6:8], 'little') & 0x3fff,
                                int.from_bytes(body[8:10], 'little') & 0x3fff)
            if kind == b'VP8L' and length >= 5 and len(body) >= 5 and body[0] == 0x2f:
                bits = int.from_bytes(body[1:5], 'little')
                return 'webp', ((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1)
            offset += 8 + length + (length & 1)
        return 'webp', (0, 0)
    return 'unknown', (0, 0)


def rejection(reason, segments):
    """Return bounded metadata for the first image without opening or decoding pixels."""
    result = {'reason': reason, 'format': 'unknown', 'source_bytes': 0,
              'source_sha256': '', 'width': 0, 'height': 0}
    validate_rejection(result)
    if not isinstance(segments, list):
        return result
    for segment in segments:
        if not isinstance(segment, dict) or segment.get('type') != 'image':
            continue
        data = segment.get('data')
        source = data.get('file') if isinstance(data, dict) else None
        if (not isinstance(source, str) or not source.startswith('base64://')
                or not 0 < len(source) - 9 <= 4 * ((MAX_DIAGNOSTIC_IMAGE + 2) // 3)):
            return result
        try:
            raw = base64.b64decode(source[9:], validate=True)
        except (binascii.Error, ValueError):
            return result
        if not 0 < len(raw) <= MAX_DIAGNOSTIC_IMAGE:
            return result
        image_format, shape = _rejection_header(raw)
        result.update(format=image_format, source_bytes=len(raw), source_sha256=hashlib.sha256(raw).hexdigest(),
                      width=shape[0] if 0 <= shape[0] <= 65535 else 0,
                      height=shape[1] if 0 <= shape[1] <= 65535 else 0)
        return validate_rejection(result)
    return result


def _png(raw):
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise OutputError('output_image_format')
    offset, width, height = 8, 0, 0
    compressed, palette, ended = [], False, False
    depth = color = None
    while offset + 12 <= len(raw):
        length = int.from_bytes(raw[offset:offset + 4], 'big')
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(raw) or not kind.isalpha():
            raise OutputError('output_image_format')
        body = memoryview(raw)[offset + 8:offset + 8 + length]
        crc = int.from_bytes(raw[offset + 8 + length:end], 'big')
        if zlib.crc32(body, zlib.crc32(kind)) & 0xffffffff != crc:
            raise OutputError('output_image_format')
        if offset == 8:
            if kind != b'IHDR' or length != 13:
                raise OutputError('output_image_format')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', body)
            allowed = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if interlace:
                raise OutputError('output_image_interlace')
            if (depth not in allowed.get(color, set()) or compression or filtering
                    or not 0 < width <= MAX_EDGE or not 0 < height <= MAX_EDGE
                    or width * height > MAX_PIXELS):
                raise OutputError('output_image_limits')
        elif kind == b'IHDR' or kind in {b'acTL', b'fcTL', b'fdAT'}:
            raise OutputError('output_image_format')
        if kind == b'PLTE':
            if palette or not 0 < length <= 768 or length % 3 or compressed:
                raise OutputError('output_image_format')
            palette = True
        elif kind == b'IDAT':
            compressed.append(body)
        elif kind == b'IEND':
            if length or end != len(raw) or not compressed:
                raise OutputError('output_image_format')
            ended = True
            break
        offset = end
    if not ended or color == 3 and not palette:
        raise OutputError('output_image_format')
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    stride = (width * depth * channels + 7) // 8 + 1
    expected = stride * height
    try:
        decoder = zlib.decompressobj()
        produced = 0
        for pending in compressed:
            while len(pending):
                pixels = decoder.decompress(pending, min(65536, expected - produced + 1))
                if (produced + len(pixels) > expected or decoder.unused_data
                        or any(pixels[i] > 4 for i in range((-produced) % stride, len(pixels), stride))):
                    raise OutputError('output_image_format')
                produced += len(pixels)
                pending = decoder.unconsumed_tail
        if produced != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise OutputError('output_image_format')
    except zlib.error:
        raise OutputError('output_image_format') from None
    return width, height


def _jpeg(raw):
    if not raw.startswith(b'\xff\xd8'):
        raise OutputError('output_image_format')
    offset, shape, scanned = 2, None, False
    while offset + 2 <= len(raw):
        if raw[offset] != 255:
            raise OutputError('output_image_format')
        marker = raw[offset + 1]
        offset += 2
        if marker == 255:
            offset -= 1
            continue
        if marker == 0xd9:
            if shape is None or not scanned:
                raise OutputError('output_image_format')
            # JPEG ends at EOI. Preserve any application trailer in the source
            # payload and digest.
            return shape
        if marker in {0, 0xd8} or 0xd0 <= marker <= 0xd7 or offset + 2 > len(raw):
            raise OutputError('output_image_format')
        length = int.from_bytes(raw[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(raw):
            raise OutputError('output_image_format')
        if marker in {0xc0, 0xc1, 0xc2}:
            if shape is not None or length < 8 or raw[offset + 2] not in {8, 12}:
                raise OutputError('output_image_format')
            height, width = struct.unpack('>HH', raw[offset + 3:offset + 7])
            if not 0 < width <= MAX_EDGE or not 0 < height <= MAX_EDGE or width * height > MAX_PIXELS:
                raise OutputError('output_image_limits')
            shape = width, height
        if marker == 0xda:
            if shape is None or length < 6 or offset + length >= len(raw) - 2:
                raise OutputError('output_image_format')
            offset += length
            scan_data = False
            while True:
                marker_at = raw.find(b'\xff', offset)
                if marker_at < 0 or marker_at + 1 >= len(raw):
                    raise OutputError('output_image_format')
                scan_data = scan_data or marker_at > offset
                following = raw[marker_at + 1]
                if following == 0:
                    scan_data = True
                    offset = marker_at + 2  # Stuffed data byte.
                elif 0xd0 <= following <= 0xd7:
                    offset = marker_at + 2  # Restart marker is not image data.
                elif following == 255:
                    offset = marker_at + 1  # Marker fill byte.
                else:
                    if not scan_data:
                        raise OutputError('output_image_format')
                    offset, scanned = marker_at, True
                    break
            continue
        offset += length
    raise OutputError('output_image_format')


def _webp(raw):
    if (len(raw) < 20 or raw[:4] != b'RIFF' or raw[8:12] != b'WEBP'
            or int.from_bytes(raw[4:8], 'little') + 8 != len(raw)):
        raise OutputError('output_image_format')
    offset, shape, canvas = 12, None, None
    while offset + 8 <= len(raw):
        kind = raw[offset:offset + 4]
        size = int.from_bytes(raw[offset + 4:offset + 8], 'little')
        end = offset + 8 + size
        if end > len(raw) or kind in {b'ANIM', b'ANMF'}:
            raise OutputError('output_image_format')
        body = raw[offset + 8:end]
        if kind == b'VP8X':
            if canvas is not None or offset != 12 or size != 10 or body[0] & 0xc3 or body[1:4] != b'\0\0\0':
                raise OutputError('output_image_format')
            canvas = (int.from_bytes(body[4:7], 'little') + 1, int.from_bytes(body[7:10], 'little') + 1)
        elif kind in {b'VP8 ', b'VP8L'}:
            if shape is not None:
                raise OutputError('output_image_format')
            if kind == b'VP8 ':
                if size < 10 or body[0] & 1 or body[3:6] != b'\x9d\x01\x2a':
                    raise OutputError('output_image_format')
                shape = (int.from_bytes(body[6:8], 'little') & 0x3fff,
                         int.from_bytes(body[8:10], 'little') & 0x3fff)
            else:
                if size < 5 or body[0] != 0x2f:
                    raise OutputError('output_image_format')
                bits = int.from_bytes(body[1:5], 'little')
                if bits >> 29:
                    raise OutputError('output_image_format')
                shape = ((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1)
        elif kind not in {b'ALPH', b'ICCP', b'EXIF', b'XMP '}:
            raise OutputError('output_image_format')
        offset = end + (size & 1)
    if offset != len(raw) or shape is None or canvas is not None and shape != canvas:
        raise OutputError('output_image_format')
    if not 0 < shape[0] <= MAX_EDGE or not 0 < shape[1] <= MAX_EDGE or shape[0] * shape[1] > MAX_PIXELS:
        raise OutputError('output_image_limits')
    return shape


def dimensions(raw, mime):
    decoder = {'image/png': _png, 'image/jpeg': _jpeg, 'image/webp': _webp}.get(mime)
    if decoder is None:
        raise OutputError('output_image_format')
    return decoder(raw)


def payload(raw, mime=None):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_IMAGE:
        raise OutputError('output_image_size')
    detected = ('image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n')
                else 'image/jpeg' if raw.startswith(b'\xff\xd8')
                else 'image/webp' if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP' else None)
    if detected is None or mime is not None and mime != detected:
        raise OutputError('output_image_format')
    dimensions(raw, detected)
    return {'mime': detected, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'quality': 'original', 'base64': base64.b64encode(raw).decode('ascii')}


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'mime', 'bytes', 'sha256', 'quality', 'base64'}
            or value.get('mime') not in MIMES or value.get('quality') != 'original'
            or type(value.get('bytes')) is not int or not 0 < value['bytes'] <= MAX_IMAGE
            or not isinstance(value.get('base64'), str)
            or len(value['base64']) != 4 * ((value['bytes'] + 2) // 3)
            or not isinstance(value.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
        raise OutputError('output_image_payload')
    try:
        raw = base64.b64decode(value['base64'], validate=True)
    except (binascii.Error, ValueError):
        raise OutputError('output_image_base64') from None
    if payload(raw, value['mime']) != value:
        raise OutputError('output_image_integrity')
    return value


def from_segment(segment):
    if (not isinstance(segment, dict) or set(segment) != {'type', 'data'} or segment.get('type') != 'image'
            or not isinstance(segment.get('data'), dict) or set(segment['data']) != {'file'}):
        raise OutputError('output_image_segment')
    source = segment['data']['file']
    if not isinstance(source, str) or not source.startswith('base64://') or len(source) == 9:
        raise OutputError('output_image_source')
    if len(source) - 9 > 4 * ((MAX_IMAGE + 2) // 3):
        raise OutputError('output_image_size')
    try:
        raw = base64.b64decode(source[9:], validate=True)
    except (binascii.Error, ValueError):
        raise OutputError('output_image_base64') from None
    return payload(raw)


def metadata(image):
    return {k: image[k] for k in ('mime', 'bytes', 'sha256', 'quality')}


def _transfer_size(meta):
    if (not isinstance(meta, dict) or type(meta.get('bytes')) is not int
            or not 0 < meta['bytes'] <= MAX_IMAGE):
        raise OutputError('output_image_metadata')
    return meta['bytes']


def assembly_window(meta):
    chunks = (_transfer_size(meta) + CHUNK_BYTES - 1) // CHUNK_BYTES
    return min(MAX_ASSEMBLY_SECONDS, ASSEMBLY_BASE_SECONDS + chunks * CHUNK_SECONDS)


def transfer_window(meta):
    return assembly_window(meta) + SUBMIT_SECONDS + TRANSFER_GRACE_SECONDS


class Assembly:
    """One strictly ordered transfer, bounded independently of pipe frames."""
    def __init__(self, meta):
        if (not isinstance(meta, dict) or set(meta) != {'mime', 'bytes', 'sha256', 'quality'}
                or meta.get('mime') not in MIMES or meta.get('quality') != 'original'
                or type(meta.get('bytes')) is not int or not 0 < meta['bytes'] <= MAX_IMAGE
                or not isinstance(meta.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', meta['sha256'])):
            raise OutputError('output_image_metadata')
        self.meta, self.raw, self.next = dict(meta), bytearray(), 0

    def add(self, index, encoded):
        if (type(index) is not int or index != self.next or not isinstance(encoded, str)
                or not 0 < len(encoded) <= 4 * ((CHUNK_BYTES + 2) // 3)):
            raise OutputError('output_image_chunk')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise OutputError('output_image_chunk') from None
        expected = min(CHUNK_BYTES, self.meta['bytes'] - len(self.raw))
        if len(raw) != expected or expected <= 0 or base64.b64encode(raw).decode('ascii') != encoded:
            raise OutputError('output_image_chunk')
        self.raw.extend(raw)
        self.next += 1

    def finish(self):
        if len(self.raw) != self.meta['bytes'] or hashlib.sha256(self.raw).hexdigest() != self.meta['sha256']:
            raise OutputError('output_image_integrity')
        value = payload(bytes(self.raw), self.meta['mime'])
        if metadata(value) != self.meta:
            raise OutputError('output_image_integrity')
        return value


def job_id(value):
    try:
        if not isinstance(value, str) or not value.startswith('job_'):
            return False
        identity = uuid.UUID(value[4:])
        return identity.version == 4 and str(identity) == value[4:]
    except (ValueError, AttributeError):
        return False


def receipt_bound(receipt, account, destination, source_sha256):
    if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS
            or receipt.get('account') != account or receipt.get('destination') != destination
            or receipt.get('source_sha256') != source_sha256
            or receipt.get('state') not in {'accepted', 'failed', 'needs_review', 'unknown', 'rejected'}
            or type(receipt.get('projection_verified')) is not bool
            or not isinstance(receipt.get('job_id'), str)
            or receipt['job_id'] and not job_id(receipt['job_id'])
            or not isinstance(receipt.get('server_id'), str)):
        return False
    server = receipt['server_id']
    if server and (not re.fullmatch('[1-9][0-9]{0,19}', server) or int(server) > 2**64 - 1):
        return False
    if receipt['state'] == 'rejected' and (receipt['job_id'] or server or receipt['projection_verified']):
        return False
    return True


def accepted(receipt, account, destination, source_sha256):
    return (receipt_bound(receipt, account, destination, source_sha256)
            and receipt['state'] == 'accepted' and bool(receipt['job_id'])
            and bool(receipt['server_id']) and receipt['projection_verified'] is True)
