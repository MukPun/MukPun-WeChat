"""WeChat validation and composition for formal host and legacy constructors."""
import time
from ...onebot.service import OneBotService
from .messages import text
from .policy import WeChatPolicy


def service_options(config, peer, *, clock=time.time, groups=False,
                   prefix='/ai ', segment_bytes=8192, private_name=None, wake_mode='prefix',
                   self_sender_id=None, image_input=False, group_context=False,
                   image_output=False, all_groups=False, voice_output=False):
    if not text(peer, 256) or peer.endswith('@chatroom'):
        raise ValueError('invalid_peer')
    if self_sender_id is not None and (not text(self_sender_id, 256)
            or self_sender_id == peer or self_sender_id.endswith('@chatroom')):
        raise ValueError('invalid_self_sender')
    if type(all_groups) is not bool:
        raise ValueError('all_groups_policy')
    if groups and ((config.room_id is None and not all_groups) or prefix != '/ai '):
        raise ValueError('group_route_required')
    if wake_mode not in {'prefix', 'mention_or_prefix', 'mention'}:
        raise ValueError('invalid_wake_mode')
    if type(group_context) is not bool:
        raise ValueError('group_context_policy')
    policy = WeChatPolicy(config.account, peer, group=config.room_id if groups else None,
        prefix=prefix, wake_mode=wake_mode, self_sender_id=self_sender_id or config.self_native_id,
        group_context=group_context, all_groups=all_groups)
    return dict(policy=policy, clock=clock,
        groups=groups, all_groups=all_groups, private_name=private_name, segment_bytes=segment_bytes,
        image_input=image_input, image_output=image_output, voice_output=voice_output)


def create_service(config, peer, state, backend, **options):
    return OneBotService(config, peer, state, backend, **service_options(config, peer, **options))
