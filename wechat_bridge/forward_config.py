"""Versioned non-secret configuration; no arbitrary endpoints or shell options."""
import json

MAX_RUNTIME = 604800
DEFAULT = {'schemaVersion': 1, 'privateContact': '测试联系人', 'groupName': '测试群',
           'groupPrefix': '/ai ', 'runtimeSeconds': 604200, 'sendIntervalSeconds': 5,
           'segmentBytes': 8192, 'groupWakeMode': 'mention_or_prefix', 'imageInputEnabled': False,
           'groupContextEnabled': False, 'imageOutputEnabled': False}


def validate(value):
    if isinstance(value, dict) and 'imageOutputEnabled' not in value:
        value = {**value, 'imageOutputEnabled': False}
    if isinstance(value, dict) and 'groupContextEnabled' not in value:
        value = {**value, 'groupContextEnabled': False}
    if isinstance(value, dict) and 'imageInputEnabled' not in value:
        value = {**value, 'imageInputEnabled': False}
    if isinstance(value, dict) and set(value) == set(DEFAULT) - {'groupWakeMode'}:
        value = {**value, 'groupWakeMode': 'prefix'}
    if not isinstance(value, dict) or set(value) != set(DEFAULT) or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
        raise ValueError('invalid_forwarder_config')
    for key in ('privateContact', 'groupName'):
        name = value[key]
        if (not isinstance(name, str) or not 1 <= len(name.encode('utf-8')) <= 256
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError('invalid_route_name')
    if (type(value['imageInputEnabled']) is not bool or type(value['groupContextEnabled']) is not bool
            or type(value['imageOutputEnabled']) is not bool
            or not isinstance(value['groupWakeMode'], str) or value['groupWakeMode'] not in {'prefix', 'mention', 'mention_or_prefix'}
            or value['groupPrefix'] != '/ai ' or type(value['runtimeSeconds']) is not int
            or not 600 <= value['runtimeSeconds'] <= MAX_RUNTIME - 600
            or type(value['sendIntervalSeconds']) is not int or not 5 <= value['sendIntervalSeconds'] <= 60
            or type(value['segmentBytes']) is not int or not 1024 <= value['segmentBytes'] <= 8192):
        raise ValueError('invalid_forwarder_limits')
    return dict(value)


def read(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 4096:
        raise ValueError('invalid_config_file')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_config_key')
            result[key] = value
        return result
    def reject(_):
        raise ValueError('invalid_constant')
    return validate(json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject))
