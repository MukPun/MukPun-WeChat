"""One validation boundary before dispatch, with unchanged public response shapes."""
import json
from ..storage import StateError
from .codec import number, result, UnsupportedMessageSegment
from . import actions


class ActionRegistry:
    def __init__(self, service, *, legacy=False):
        self.service = service
        self.handlers = {**actions.STATUS, **actions.DIRECTORY}
        self.handlers.update({name: actions.legacy_send if legacy else actions.send
                             for name in ('send_msg', 'send_group_msg')})
        if not legacy:
            self.handlers.update(send_private_msg=actions.send, get_friend_list=actions.friends)

    async def dispatch(self, request):
        try:
            if not isinstance(request, dict) or not set(request) <= {'action', 'params', 'echo'}:
                raise ValueError('invalid_request')
            if 'echo' in request and len(json.dumps(request['echo'], ensure_ascii=True, allow_nan=False)) > 4096:
                raise ValueError('echo_too_large')
            action, params = request.get('action'), request.get('params', {})
            if not isinstance(action, str) or not isinstance(params, dict):
                raise ValueError('invalid_request')
            if 'self_id' in params and number(params['self_id']) != self.service.self_id:
                raise ValueError('self_id_mismatch')
            handler = self.handlers.get(action)
            answer = (await handler(self.service, action, params, request) if handler else
                      result(1501, message='action_not_supported'))
        except UnsupportedMessageSegment:
            answer = result(1400, message='unsupported_message_segment')
        except StateError:
            answer = result(1500, message='state_unavailable')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            answer = result(1400, message='invalid_request')
        if isinstance(request, dict) and 'echo' in request:
            answer = {**answer, 'echo': request['echo']}
        return answer
