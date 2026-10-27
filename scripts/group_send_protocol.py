"""One fixed group text, using a private verified binding and persisted key.

The caller must obtain human approval and save the one-attempt audit before
admission. This reuses the tested send receipt/polling and HTTP implementation.
No login, group refresh, private message or submission retry is available here.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time


def companion(name):
    spec = importlib.util.spec_from_file_location('_group_send_' + name, Path(__file__).with_name(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


base = companion('contact_send_protocol')
groups = companion('group_receive_protocol')
TEST_TEXT = '微信接入测试：这是一条群聊发送验证消息，无需回复。'
RESULT_CODES = base.RESULT_CODES
SendError, SendResult, Response = base.SendError, base.SendResult, base.Response


def valid_room(value):
    return (base.receive._text(value, 256) and value.endswith('@chatroom') and value != '@chatroom')


class RequestPolicy(base.RequestPolicy):
    def bind(self, target, key):
        if (self._body is not None or not valid_room(target) or not isinstance(key, str)
                or not base.KEY_RE.fullmatch(key)):
            raise SendError('request_not_allowed')
        self._body = json.dumps({'threadId': target, 'text': TEST_TEXT}, ensure_ascii=False,
                                sort_keys=True, separators=(',', ':')).encode('utf-8')
        self._headers = {'Content-Type': 'application/json', 'Idempotency-Key': key}


class GroupSendClient(base.ContactSendClient):
    POLICY_TYPE = RequestPolicy
    valid_target = staticmethod(valid_room)

    def __init__(self, account, token, room_id, target_name, idempotency_key, *, port=26174,
                 transport=None, lookup=None, clock=time.monotonic, sleep=time.sleep, on_intent=None):
        if (not valid_room(room_id) or not base.receive._text(target_name, 256)
                or not isinstance(idempotency_key, str) or not base.KEY_RE.fullmatch(idempotency_key)
                or on_intent is not None and not callable(on_intent)
                or lookup is not None and not callable(lookup)):
            raise SendError('invalid_arguments')
        self._room_id, self._fixed_key = room_id, idempotency_key
        self._on_intent, self._intent_reported = on_intent, False
        reader = groups.GroupReceiveClient(account, token, target_name, port=port, clock=clock, sleep=sleep)
        resolve = reader._target if lookup is None else lookup

        def bound_lookup(deadline):
            try:
                match = resolve(deadline)
            except groups.GroupError as error:
                raise SendError(error.code) from None
            if not isinstance(match, tuple) or len(match) != 2 or match[0] != room_id:
                raise SendError('invalid_contacts')
            return match

        super().__init__(account, token, port=port, transport=transport, lookup=bound_lookup,
                         clock=clock, sleep=sleep)

    def _new_key(self):
        return self._fixed_key

    def _intent(self, value, target):
        state = super()._intent(value, target)
        if not self._intent_reported and self._on_intent is not None:
            self._intent_reported = True
            try:
                self._on_intent(self._send_id)
            except Exception:
                raise SendError('callback_failed') from None
        return state
