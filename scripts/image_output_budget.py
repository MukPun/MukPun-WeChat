"""Finite persistent image-upload budget, independent of any live services."""
MAX_ATTEMPTS = 1000000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_INPUT_BYTES = 128 * 1024
STORAGE_RESERVE = 16 * 1024 * 1024
ATTEMPT_STORAGE = 1024 * 1024
MEMORY_RESERVE = 64 * 1024 * 1024
FILE_LIMIT = 16 * 1024 * 1024


def allow_input(*, storage_free, memory_free, database_bytes, file_limit, database_copies=0):
    """Pause new SDK media work early, reserving room for text and metadata."""
    return (database_copies in (0, 2) and storage_free >= max(32*1024*1024,
            STORAGE_RESERVE + (database_copies+1)*(MAX_INPUT_BYTES+ATTEMPT_STORAGE))
            and memory_free >= MEMORY_RESERVE
            and file_limit-database_bytes >= 32*1024*1024)


def reserve(value, source_bytes, *, storage_free, memory_free, database_bytes=0, file_limit=FILE_LIMIT, reused_source=False, database_copies=0, source_limit=MAX_SOURCE_BYTES):
    if value is None:
        value = {'schemaVersion': 1, 'attempts': 0, 'sourceBytes': 0}
    if (type(source_limit) is not int or source_limit not in (MAX_SOURCE_BYTES, 9600044)
            or not isinstance(value, dict) or set(value) != {'schemaVersion', 'attempts', 'sourceBytes'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or type(value['attempts']) is not int or not 0 <= value['attempts'] <= MAX_ATTEMPTS
            or type(value['sourceBytes']) is not int or not 0 <= value['sourceBytes'] <= MAX_ATTEMPTS * source_limit
            or type(source_bytes) is not int or not 0 < source_bytes <= source_limit
            or type(storage_free) is not int or type(memory_free) is not int
            or type(database_bytes) is not int or database_bytes < 0
            or type(file_limit) is not int or file_limit not in {16*1024*1024,32*1024*1024,128*1024*1024}
            or type(reused_source) is not bool
            or type(database_copies) is not int or database_copies not in (0, 2)):
        raise ValueError('image_budget_invalid')
    # Voice stores the WAV, a sealed JSON byte-array SILK plan, encoded/decoded
    # assets and official voice cache. Three source copies plus 2MiB covers
    # the pinned maximum 186010-byte note, five-note plan and RIFF padding.
    growth = (3 * source_bytes + 2 * 1024 * 1024 if source_limit != MAX_SOURCE_BYTES
              else ATTEMPT_STORAGE if reused_source else max(ATTEMPT_STORAGE, 2 * source_bytes + 512 * 1024))
    if (value['attempts'] >= MAX_ATTEMPTS or storage_free < STORAGE_RESERVE + (database_copies+1)*growth
            or database_bytes + growth + 256 * 1024 > file_limit
            or memory_free < MEMORY_RESERVE):
        return None
    return {'schemaVersion': 1, 'attempts': value['attempts'] + int(not reused_source),
            'sourceBytes': value['sourceBytes'] + (0 if reused_source else source_bytes)}


def usable_storage(total_bytes, free_bytes, database_bytes):
    """Keep 1.2.0's startup scratch out of the normal media budget."""
    if total_bytes != 192*1024*1024:
        return free_bytes
    return max(0, min(free_bytes-64*1024*1024, free_bytes-2*database_bytes))
