"""Finite persistent image-upload budget, independent of any live services."""
MAX_ATTEMPTS = 1000000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
STORAGE_RESERVE = 16 * 1024 * 1024
ATTEMPT_STORAGE = 1024 * 1024
MEMORY_RESERVE = 64 * 1024 * 1024
FILE_LIMIT = 16 * 1024 * 1024


def allow_input(*, storage_free, memory_free, database_bytes, file_limit):
    """Pause new SDK media work early, reserving room for text and metadata."""
    return (storage_free >= 32*1024*1024 and memory_free >= MEMORY_RESERVE
            and file_limit-database_bytes >= 32*1024*1024)


def reserve(value, source_bytes, *, storage_free, memory_free, database_bytes=0, file_limit=FILE_LIMIT, reused_source=False):
    if value is None:
        value = {'schemaVersion': 1, 'attempts': 0, 'sourceBytes': 0}
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'attempts', 'sourceBytes'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or type(value['attempts']) is not int or not 0 <= value['attempts'] <= MAX_ATTEMPTS
            or type(value['sourceBytes']) is not int or not 0 <= value['sourceBytes'] <= MAX_ATTEMPTS * MAX_SOURCE_BYTES
            or type(source_bytes) is not int or not 0 < source_bytes <= MAX_SOURCE_BYTES
            or type(storage_free) is not int or type(memory_free) is not int
            or type(database_bytes) is not int or database_bytes < 0
            or type(file_limit) is not int or file_limit not in {16*1024*1024,32*1024*1024,128*1024*1024}
            or type(reused_source) is not bool):
        raise ValueError('image_budget_invalid')
    growth = ATTEMPT_STORAGE if reused_source else max(ATTEMPT_STORAGE, 2 * source_bytes + 512 * 1024)
    if (value['attempts'] >= MAX_ATTEMPTS or storage_free < STORAGE_RESERVE + growth
            or database_bytes + growth + 256 * 1024 > file_limit
            or memory_free < MEMORY_RESERVE):
        return None
    return {'schemaVersion': 1, 'attempts': value['attempts'] + int(not reused_source),
            'sourceBytes': value['sourceBytes'] + (0 if reused_source else source_bytes)}
