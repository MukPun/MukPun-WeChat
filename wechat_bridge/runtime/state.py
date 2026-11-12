"""Controller lifetime data; reset() replaces all per-generation state."""
from dataclasses import dataclass, field
import time


@dataclass
class State:
    settings: object = None
    pinned: object = None
    counters: object = field(default_factory=dict)
    route_counts: object = field(default_factory=dict)
    queue_counts: object = field(default_factory=dict)
    loop_counts: object = field(default_factory=dict)
    image_counts: object = field(default_factory=dict)
    group_traffic: object = field(default_factory=dict)
    last_image_error: object = None
    image_reviews: object = field(default_factory=list)
    last_output_error: object = None
    self_sender_id: object = None
    largest_reply: object = field(default_factory=dict)
    resources: object = field(default_factory=dict)
    onebot_link: object = field(default_factory=dict)
    last_activity_at: object = None
    good: object = False
    code: object = 'starting'
    export_state: object = None
    media_lease: object = None
    seconds: object = None
    room_id: object = None
    bundle: object = None
    legacy_generation: object = None
    generation: object = None
    host_wait: object = field(default_factory=lambda: {'ready': False, 'last': time.monotonic()})
    recovery_seed: object = None
    host_digest: object = None
    peer_id: object = None
    boundary: object = None
    isolated: object = field(default_factory=set)
    armed: object = False
    sse_connected: object = False
    astrbot_connected: object = False
    request: object = None
    image_transfer: object = None
    input_relay: object = None
    image_ack: object = None
    audit: object = None
    image_audit: object = None
    audit_path: object = None
    image_audit_path: object = None
    last_input: object = field(default_factory=dict)
    recovery_adapter: object = None
    recovery_path: object = None
    recovery_refresh: object = False
    recovery_capacity: object = False
    recovery_next: object = 0.
    recovery_state: object = 'pending'
    cache_path: object = None
    source_cache: object = None
    voice_relay: object = None
    activated: bool = False
    ready_committed: bool = False
    workers: object = None
