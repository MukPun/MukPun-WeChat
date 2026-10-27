"""Finite persistent image-upload budget, independent of any live services."""
MAX_ATTEMPTS = 16
MAX_SOURCE_BYTES = 2 * 1024 * 1024
STORAGE_RESERVE = 8 * 1024 * 1024
ATTEMPT_STORAGE = 1024 * 1024
MEMORY_RESERVE = 32 * 1024 * 1024
FILE_LIMIT = 16 * 1024 * 1024


def reserve(value, source_bytes, *, storage_free, memory_free, database_bytes=0):
    if value is None:
        value = {'schemaVersion': 1, 'attempts': 0, 'sourceBytes': 0}
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'attempts', 'sourceBytes'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or type(value['attempts']) is not int or not 0 <= value['attempts'] <= MAX_ATTEMPTS
            or type(value['sourceBytes']) is not int or not 0 <= value['sourceBytes'] <= MAX_ATTEMPTS * MAX_SOURCE_BYTES
            or type(source_bytes) is not int or not 0 < source_bytes <= MAX_SOURCE_BYTES
            or type(storage_free) is not int or type(memory_free) is not int
            or type(database_bytes) is not int or database_bytes < 0):
        raise ValueError('image_budget_invalid')
    growth = max(ATTEMPT_STORAGE, 2 * source_bytes + 512 * 1024)
    if (value['attempts'] >= MAX_ATTEMPTS or storage_free < STORAGE_RESERVE + growth
            or database_bytes + growth + 256 * 1024 > FILE_LIMIT
            or memory_free < MEMORY_RESERVE):
        return None
    return {'schemaVersion': 1, 'attempts': value['attempts'] + 1,
            'sourceBytes': value['sourceBytes'] + source_bytes}
