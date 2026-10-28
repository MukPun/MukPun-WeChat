"""GET-only nickname metadata probe, with no SSE or messaging actions."""
import time
import importlib.util
from pathlib import Path
import sys
import http.client
import json
import uuid
from urllib.parse import quote

spec = importlib.util.spec_from_file_location('_nickname_probe_native', Path(__file__).with_name('nickname_probe_runtime.py'))
native = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = native
spec.loader.exec_module(native)


def main():
    native.reader.CODE_FILES += ('nickname_probe_runtime.py',)
    config = native.wire.decode(native.sys.stdin.buffer.readline(4097))
    assert set(config) == {'account', 'token', 'rooms', 'users', 'refresh', 'inspectEvents', 'after'} and 0 < len(config['rooms']) <= 3
    checks = native.reader.isolation_checks()
    native.emit({'kind': 'isolated', 'ok': all(checks.values()), 'checks': checks})
    if not all(checks.values()) or not native.reader.await_ready():
        return 1
    client = native.contact.ContactReceiveClient(config['account'], config['token'])
    if config['inspectEvents']:
        probes = {}
        paths = [('account', client.policy.prefix),
                 ('events', client.policy.prefix + '/events?after=' + native.sse.cursor(config['after']) + '&limit=20')]
        paths += [('thread_' + str(index), client.policy.prefix + '/threads/' + quote(room, safe='') + '/messages?limit=20')
                  for index, room in enumerate(config['rooms'])]
        for label, path in paths:
            client.policy.path = path
            try:
                value = client._get(path, time.monotonic() + 4, timeout=3)
                entry = {'ok': True}
                if label == 'account':
                    entry.update({key: value.get(key) for key in ('connection', 'authenticated')})
                    entry['fields'] = sorted(value)
                else:
                    entry['items'] = [{key: item.get(key) for key in ('cursor', 'kind', 'createdAt', 'direction', 'origin', 'type') if key in item}
                                      for item in value.get('items', [])]
                    entry['fields'] = sorted(value)
                    entry['image_assets'] = []
                    media = native.images.Client(config['account'], config['token'])
                    for item in value.get('items', []):
                        if label.startswith('thread_') and item.get('type') == 3 and item.get('direction') == 'incoming' and len(entry['image_assets']) < 2:
                            try:
                                assets = media.assets(item['id'], time.monotonic() + 4)
                                entry['image_assets'].append({'time': item.get('createdAt'), 'variants': [
                                    {key: asset.get(key) for key in ('kind', 'variant', 'actualVariant', 'state', 'byteLength', 'contentType')}
                                    for asset in assets]})
                            except native.images.ImageError as error:
                                entry['image_assets'].append({'time': item.get('createdAt'), 'error': str(error) if str(error) in native.images.ERROR_CODES else 'image_failed'})
                probes[label] = entry
            except native.contact.ContactError as error:
                probes[label] = {'ok': False, 'code': error.code}
        native.emit({'kind': 'nickname_probe', 'after': config['after'], 'events_diagnostic': probes})
        return 0
    results = []
    for room in config['rooms']:
        assert native.trigger.group_id(room)
        if config['refresh']:
            connection = http.client.HTTPConnection('127.0.0.1', 26174, timeout=3)
            try:
                connection.request('POST', client.policy.prefix + '/groups/' + quote(room, safe='') + '/members/refresh',
                    headers={'Authorization': 'Bearer ' + config['token'], 'Accept': 'application/json'})
                response = connection.getresponse()
                operation = json.loads(response.read(65537))
                assert response.status == 202
                identity = operation['id']
                assert uuid.UUID(identity).version == 4
            finally:
                connection.close()
            client.policy.path = client.policy.prefix + '/operations/' + quote(identity, safe='')
            for _ in range(60):
                operation = client._get(client.policy.path, time.monotonic() + 3, timeout=2)
                if operation.get('state') in {'succeeded', 'failed', 'cancelled'}:
                    break
                time.sleep(.3)
            native.emit({'kind': 'nickname_refresh', 'state': operation.get('state'),
                         'member_count': (operation.get('result') or {}).get('memberCount'),
                         'projection': (operation.get('result') or {}).get('profileProjection')})
        client.policy.path = client.policy.prefix + '/groups/' + quote(room, safe='') + '/members?limit=100'
        try:
            page = client._get(client.policy.path, time.monotonic() + 4, timeout=3)
            items = [item for item in page.get('items', []) if isinstance(item, dict) and not item.get('deleted')]
            named = [item for item in items if item.get('displayName') and item['displayName'] != item.get('id')]
            cards = [item for item in items if item.get('roomDisplayName') and item['roomDisplayName'] != item.get('id')]
            entry = {'ok': True, 'members': len(items), 'named': len(named), 'cards': len(cards),
                     'partial': page.get('coverage', {}).get('partial'), 'has_next': bool(page.get('nextCursor')),
                     'fields': sorted(set().union(*(item.keys() for item in items)))}
            candidate = next((item for item in items if not item.get('displayName') or item.get('displayName') == item.get('id')), None)
            if candidate:
                client.policy.path = client.policy.prefix + '/contacts?search=' + quote(candidate['id'], safe='') + '&limit=20'
                try:
                    contact_page = client._get(client.policy.path, time.monotonic() + 3, timeout=2)
                    contacts = [item for item in contact_page.get('items', []) if item.get('id') == candidate['id']]
                    entry['contact_match'] = len(contacts)
                    entry['contact_named'] = sum(bool(item.get('nickname') or item.get('displayName') != item.get('id')) for item in contacts)
                except native.contact.ContactError as error:
                    entry['contact_error'] = error.code
            results.append(entry)
        except native.contact.ContactError as error:
            results.append({'ok': False, 'code': error.code})
    users = []
    for user in config['users']:
        client.policy.path = client.policy.prefix + '/contacts?search=' + quote(user, safe='') + '&limit=20'
        try:
            page = client._get(client.policy.path, time.monotonic() + 3, timeout=2)
            items = [item for item in page.get('items', []) if item.get('id') == user and not item.get('deleted')]
            users.append({'ok': True, 'matched': len(items),
                          'nickname': any(bool(item.get('nickname')) and item['nickname'] != user for item in items),
                          'display_name': any(bool(item.get('displayName')) and item['displayName'] != user for item in items),
                          'remark': any(bool(item.get('remark')) for item in items)})
        except native.contact.ContactError as error:
            users.append({'ok': False, 'code': error.code})
    resolution = []
    if hasattr(native, 'sender_names'):
        resolver = native.sender_names.Resolver(native.contact.ContactReceiveClient(config['account'], config['token']))
        for user in config['users']:
            started = time.monotonic()
            names = resolver.resolve(config['rooms'][0], user)
            resolution.append({'named': bool(names.get('sender_nickname')), 'card': bool(names.get('sender_card')),
                               'seconds': round(time.monotonic() - started, 2)})
    native.emit({'kind': 'nickname_probe', 'groups': results, 'users': users, 'production_resolver': resolution})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
