"""Bounded UTF-8 text validation and lossless delivery splitting."""
def text(value, maximum=8192):
    try:
        return (isinstance(value, str) and 0 < len(value.encode('utf-8')) <= maximum
                and not any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in value))
    except UnicodeError:
        return False

def split_text(value, maximum=8192):
    if not text(value.replace("\r", "\n") if isinstance(value, str) else value, 32768) or not value.strip():
        raise ValueError('invalid_message')
    parts, current, size = [], [], 0
    for char in value:
        length = len(char.encode('utf-8'))
        if size + length > maximum:
            parts.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += length
    if current:
        parts.append(''.join(current))
    return parts
