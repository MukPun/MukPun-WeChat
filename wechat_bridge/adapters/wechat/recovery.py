"""Pinned WeChat evidence and synchronous checkpoint recovery backend."""
import time


class RecoveryBackend:
    def __init__(self, core, state):
        self.core, self.state = core, state

    def capture_if_safe(self):
        core, s = self.core, self.state
        # This is the single forwarding loop: while the synchronous capture
        # runs, no Native frame or host ACK can be forwarded. Already sent
        # frames are the sole ACK-gated pre-snapshot source. Do not move this
        # call into the owner callback, probe thread or async background task.
        if (s.armed and s.recovery_adapter is not None and s.request is None and not s.voice_relay.pending
                and not s.input_relay.transferring and s.image_transfer is None):
            notification = core.owner.CONTROL/'recovery-refresh.json'
            if notification.exists():
                core.require(core.owner.read_json(notification) == {'kind':'recovery_refresh'}, 'recovery_notification')
                notification.unlink()
                s.recovery_refresh = True
            if s.recovery_refresh or time.monotonic() >= s.recovery_next:
                try:
                    with core.recovery_checkpoints.bridge(s.recovery_path) as live:
                        previous_floor = live.recovery_coverage()['floorSeq']
                    # Previous still protects its floor. Capacity requires
                    # two DISTINCT fresh completed captures to rotate it.
                    for _ in range(2 if s.recovery_capacity else 1):
                        pair = core.recovery_checkpoints.capture(s.recovery_adapter,s.recovery_path,core.owner.STATE)
                        # This safe point excludes broker ingress; all proof
                        # comes from authenticated GET against this owner.
                        reconcile = core.recovery_coordinator.Production(core,bridge_path=s.recovery_path)
                        try:
                            reconcile.reconcile(pair,s.pinned,s.generation,s.recovery_seed,s.self_sender_id)
                        except ValueError:
                            # A failed proof must not fall through to an empty
                            # replay barrier and overwrite review with ready.
                            # Nor may a second capture rotate its protection.
                            raise ValueError('recovery_reconcile') from None
                    with core.recovery_checkpoints.bridge(s.recovery_path) as live:
                        coverage = live.recovery_coverage()
                        if s.recovery_capacity:
                            if not (coverage['floorSeq'] > previous_floor and
                                live.db.execute('SELECT count(*) FROM inbox WHERE seq>=?',(coverage['floorSeq'],)).fetchone()[0] < 4096):
                                raise ValueError('recovery_capacity_requires_review')
                    s.workers.peers['host'].send({'kind':'recovery_protection','floorSeq':coverage['floorSeq'],'epoch':coverage['epoch']})
                    with core.recovery_checkpoints.bridge(s.recovery_path) as live:
                        s.recovery_state = 'requires_review' if core.recovery_checkpoints.replay_barrier(live) else 'ready'
                    s.recovery_capacity = False
                except (ValueError, RuntimeError):
                    s.recovery_state = 'capacity_requires_review' if s.recovery_capacity else 'requires_review'
                s.recovery_refresh = False
                s.recovery_next = time.monotonic()+core.owner.CHECKPOINT_SECONDS

    def retire(self):
        """Persist interrupted output and publishing retirement before respawn."""
        core, s = self.core, self.state
        if s.request is not None:
            media = s.request.get('kind') in {'send_image', 'image_begin'}
            audit = s.image_audit if media else s.audit
            path = s.image_audit_path if media else s.audit_path
            core.require(isinstance(audit, dict), 'send_requires_review')
            if not core.audit_cleared(audit):
                audit.update(state='unknown')
                core.settle_outcome(s.pinned, path, audit, 'media' if media else 'text',
                                    'unconfirmed_quarantined')
            # Recording unknown evidence is not permission to reopen a gate.
            core.require(core.audit_cleared(audit), 'send_requires_review')
        if s.voice_relay.pending:
            s.voice_relay.quarantine('voice_response')
        with core.recovery_checkpoints.bridge(s.recovery_path) as live:
            live.quarantine_publishing()
        if s.media_lease is not None:
            s.media_lease.close()

    def reset(self):
        core, s = self.core, self.state
        # Constructor performs persisted voice preflight. Never bypass it when
        # replacing a relay whose unknown outcome was merely recorded.
        voice = core.voice_forwarding.Relay(core, s.pinned, s.settings, s.counters)
        lease = core.media_egress.Lease(core, s.pinned, hd_input=s.export_state is not None)
        s.voice_relay, s.media_lease = voice, lease
        s.input_relay = core.input_wire.Relay()
        s.request = s.image_transfer = s.image_ack = s.audit = s.image_audit = None
        s.peer_id = s.boundary = s.self_sender_id = s.recovery_adapter = None
        s.armed = s.sse_connected = s.astrbot_connected = s.activated = False
        s.isolated.clear()
        s.host_wait = {'ready': False, 'last': time.monotonic()}
        s.queue_counts = {}
        s.last_input = {}
        # Keep persisted recovery admission/review state and cached pinned
        # selfProof. No selected_pair/preflight snapshot proof is repeated.
