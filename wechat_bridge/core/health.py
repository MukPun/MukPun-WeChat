"""Nonsecret, expiring account truth and independent operational admission.

Pure Python so the controller's isolated importlib entrypoint can use the same
validation/evaluation as the OneBot worker without importing SDK or storage.
"""
import re
import time

NATIVE_FRESH_SECONDS = 2 * 30 + 15
_CONNECTIONS = {'online', 'reconnecting', 'disconnected', 'unchecked', 'unreachable'}
_RECOVERY = {'pending', 'ready', 'requires_review', 'capacity_requires_review'}
_FIELDS = {'kind', 'observed_at', 'native_checked_at', 'native_connected',
           'native_connection', 'native_probe_code', 'sse', 'astrbot_connected',
           'admitted', 'recovery'}


def validate_health(value):
    if (not isinstance(value, dict) or set(value) != _FIELDS or value['kind'] != 'health'
            or any(type(value[k]) is not bool for k in ('native_connected', 'sse', 'astrbot_connected', 'admitted'))
            or any(type(value[k]) is not int or not 0 <= value[k] <= 2**63-1
                   for k in ('observed_at', 'native_checked_at'))
            or value['observed_at'] == 0
            or value['native_checked_at'] > value['observed_at']
            or not isinstance(value['native_connection'], str) or value['native_connection'] not in _CONNECTIONS
            or not isinstance(value['recovery'], str) or value['recovery'] not in _RECOVERY
            or not isinstance(value['native_probe_code'], str)
            or re.fullmatch('[a-z][a-z0-9_]{0,63}', value['native_probe_code']) is None
            or value['native_connected'] and (value['native_connection'] != 'online'
                or value['native_probe_code'] != 'ok' or value['native_checked_at'] == 0)):
        raise ValueError('health_frame')
    return dict(value)


def evaluate_health(value, now, *, websocket=True):
    fresh = (0 <= now - value['native_checked_at'] <= NATIVE_FRESH_SECONDS
             and 0 <= now - value['observed_at'] <= NATIVE_FRESH_SECONDS)
    online = fresh and value['native_connected']
    return {'online': bool(online), 'good': bool(online and websocket and value['sse']
        and value['astrbot_connected'] and value['admitted'])}


class HealthState:
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.record = None

    def update(self, value):
        value = validate_health(value)
        if (value['observed_at'] > self.clock() or self.record is not None
                and value['observed_at'] < self.record['observed_at']):
            raise ValueError('health_observation')
        self.record = value

    def status(self, *, websocket=True):
        if self.record is None:
            return {'online': False, 'good': False}
        return evaluate_health(self.record, self.clock(), websocket=websocket)
