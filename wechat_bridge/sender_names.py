"""Optional, bounded GET-only display-name lookups; failures never stop input."""
from collections import OrderedDict
import time
from urllib.parse import quote


def clean(value, identity=None):
    if not isinstance(value, str) or not value.strip() or value == identity:
        return ''
    try:
        if len(value.encode('utf-8')) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            return ''
    except UnicodeError:
        return ''
    return value


class Resolver:
    def __init__(self, client, *, clock=time.monotonic, budget=.8, capacity=1024):
        self.client, self.clock, self.budget, self.capacity = client, clock, budget, capacity
        self.cache = OrderedDict()

    def _record(self, path, user, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            return None
        try:
            self.client.policy.path = path
            page = self.client._get(path, deadline, timeout=min(.4, remaining))
            items = page.get('items')
            if not isinstance(items, list) or len(items) > 20:
                return None
            matches = [item for item in items if isinstance(item, dict) and item.get('id') == user
                       and item.get('username') == user and not item.get('deleted')]
            return matches[0] if len(matches) == 1 else None
        except Exception:
            return None

    def resolve(self, room, user):
        key, now = (room, user), self.clock()
        saved = self.cache.get(key)
        if saved and saved[0] > now:
            self.cache.move_to_end(key)
            return dict(saved[1])
        deadline = now + self.budget
        prefix = self.client.policy.prefix
        member = self._record(prefix + '/groups/' + quote(room, safe='') + '/members?search='
                              + quote(user, safe='') + '&limit=20', user, deadline)
        contact = self._record(prefix + '/contacts?search=' + quote(user, safe='') + '&limit=20', user, deadline)
        nickname = (clean((contact or {}).get('nickname'), user)
                    or clean((contact or {}).get('displayName'), user)
                    or clean((member or {}).get('displayName'), user))
        result = {'sender_nickname': nickname} if nickname else {}
        if member is not None:
            result['sender_card'] = clean(member.get('roomDisplayName'), user)
        self.cache[key] = (self.clock() + (300 if nickname else 30), result)
        self.cache.move_to_end(key)
        while len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return dict(result)
