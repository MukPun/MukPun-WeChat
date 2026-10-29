"""Pure, conservative idle gate for offline media-source maintenance."""
PRESSURE_BYTES = 48 * 1024 * 1024
CACHE_BYTES = 500 * 1024 * 1024
MIN_GAIN = 1024 * 1024
IDLE_SECONDS = 60
FRESH_SECONDS = 15
SUCCESS_COOLDOWN = 300
FAILURE_COOLDOWN = 3600


def decision(status, capacity, previous=None, *, now):
    if previous is not None:
        if (not isinstance(previous, dict) or not isinstance(previous.get('outcome'), str)
                or previous.get('outcome') not in
                {'success', 'no_gain', 'failed'}):
            return 'requires_review'
        ended = previous.get('completed_at')
        if type(ended) is not int or not 0 < ended <= now:
            return 'requires_review'
        cooldown = SUCCESS_COOLDOWN if previous['outcome'] == 'success' else FAILURE_COOLDOWN
        if now-ended < cooldown:
            return 'cooldown'
    if not isinstance(status, dict) or not isinstance(capacity, dict):
        return 'invalid_health'
    updated, activity = status.get('updated'), status.get('last_activity_at')
    if (type(updated) is not int or not 0 <= now-updated <= FRESH_SECONDS
            or type(activity) is not int or not 0 < activity <= now):
        return 'stale'
    if any(status.get(k) is not True for k in ('active', 'ready', 'sse', 'astrbot_connected')):
        return 'not_ready'
    if (status.get('outbound_pending') is not False or status.get('image_egress_active') is not False
            or type(status.get('uncertain')) is not int or status['uncertain'] != 0):
        return 'busy'
    queue = status.get('queue')
    if (not isinstance(queue, dict) or any(type(v) is not int or v < 0 for v in queue.values())
            or any(queue.get(k, 0) for k in ('queued', 'publishing', 'uncertain'))
            or now-activity < IDLE_SECONDS):
        return 'busy'
    headroom = capacity.get('storage_free')
    used = capacity.get('storage_used')
    if type(headroom) is not int or headroom < 0 or type(used) is not int or used < 0:
        return 'invalid_health'
    return 'maintain' if headroom < PRESSURE_BYTES or used >= CACHE_BYTES else 'capacity_ok'


def worthwhile(result):
    if not isinstance(result, dict):
        return False
    keys = ('sources_removed', 'snapshot_bytes_before', 'snapshot_bytes_after')
    if any(type(result.get(k)) is not int or result[k] < 0 for k in keys):
        return False
    return (result['sources_removed'] > 0
            and result['snapshot_bytes_before']-result['snapshot_bytes_after'] >= MIN_GAIN)
