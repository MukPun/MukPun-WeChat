"""Independent handlers grouped by send, status/capability and directory actions."""
from .codec import number


async def send(service, action, params, request):
    if not service.groups and action == 'send_group_msg':
        return service.result(1403, message='destination_not_allowed')
    destination = service.destination(action, params)
    # Historically send_private_msg canonicalized to send_msg before hashing.
    request = dict(request, action='send_msg') if action == 'send_private_msg' else request
    content = service.content.prepare(destination, params)
    return await service.delivery.send(destination, content, request)


async def legacy_send(service, action, params, request):
    return await service._send(action, params, request)


async def login(service, action, params, request):
    return service.result(data={'user_id': service.self_id, 'nickname': '微信助手'})


async def status(service, action, params, request):
    return service.result(data={'online': service.online, 'good': service.online})


async def version(service, action, params, request):
    return service.result(data={'app_name': 'astrbot-wechat-bridge', 'app_version': '0.1.0', 'protocol_version': 'v11'})


async def capability(service, action, params, request):
    return service.result(data={'yes': getattr(service, 'image_output' if action == 'can_send_image' else 'voice_output', False)})


async def friends(service, action, params, request):
    return service.result(data=[{'user_id': service.peer_id, 'nickname': service.private_name, 'remark': ''}])


async def groups(service, action, params, request):
    return service.result(data=service._group_list())


async def group_info(service, action, params, request):
    group = service._group(params)
    return service.result(data={'group_id': group, 'group_name': service._group_name(group)})


async def member(service, action, params, request):
    group = service._group(params)
    user = known_user(service, params)
    return service.result(data={'group_id': group, **service._sender(group, user)})


def known_user(service, params):
    user = number(params.get('user_id'))
    if user != service.self_id and not service.state.known('user', user):
        raise ValueError('unknown_member')
    return user


async def stranger(service, action, params, request):
    user = known_user(service, params)
    return service.result(data={'user_id': user,
        'nickname': '微信助手' if user == service.self_id else service.state.user_nickname(user) or '微信成员' + str(user),
        'sex': 'unknown', 'age': 0})


async def current_message(service, action, params, request):
    if service.pending is not None and number(params.get('message_id')) == service.pending['event']['message_id']:
        return service.result(data={key: service.pending['event'][key]
            for key in ('time', 'message_type', 'message_id', 'sender', 'message')})
    return service.result(1501, message='action_not_supported')


STATUS = {'get_login_info': login, 'get_status': status, 'get_version_info': version,
          'can_send_image': capability, 'can_send_record': capability}
DIRECTORY = {'get_group_list': groups, 'get_group_info': group_info,
             'get_group_member_info': member, 'get_stranger_info': stranger, 'get_msg': current_message}
