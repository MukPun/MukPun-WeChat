"""Pinned WeChat evidence and phased checkpoint recovery backend."""
import time


class RecoveryBackend:
    def __init__(self, core, state):
        self.core, self.state = core, state
        state.recovery_backend=self

    def _safe(self, *, allow_reviews=False):
        s=self.state
        return (s.armed and s.request is None and not s.voice_relay.pending
            and not s.input_relay.transferring and s.image_transfer is None
            and s.image_ack is None)

    def barrier(self,name,record):
        s=self.state
        if record.get('kind')=='capture_deferred':
            self.core.require(name=='host' and s.capture_sent=={'host'}
                and s.capture_phase=='quiescing' and not s.capture_acks
                and record=={'kind':'capture_deferred','token':s.capture_token},'capture_binding')
            s.capture_phase=s.capture_token=None;s.capture_paused=False
            s.recovery_refresh=True
            s.capture_retry_at=time.monotonic()+5
            return
        key='fenced' if record.get('kind')=='capture_fenced' else name
        self.core.require(name in s.capture_sent and s.capture_phase=='quiescing'
            and record=={'kind':'capture_fenced' if key=='fenced' else 'capture_paused','token':s.capture_token}
            and key not in s.capture_acks
            and (key!='fenced' or name=='host' and s.capture_fence_sent),'capture_binding')
        s.capture_acks.add(key)

    def _resume(self):
        s=self.state
        for name in s.capture_sent:
            s.workers.peers[name].send({'kind':'capture_resume','token':s.capture_token})
        s.capture_paused=False

    def close(self):
        s=self.state
        if s.capture_job is not None:
            s.capture_job.close()
            s.capture_job=None
        # Worker teardown follows cancellation. Never resume into an owner
        # which could still be completing a mailbox snapshot.
        s.capture_phase=None; s.capture_token=None; s.capture_acks.clear()

    def _async_capture(self):
        from ...runtime.capture_job import CaptureJob
        import secrets
        core,s=self.core,self.state
        now=time.monotonic()
        try:
            if s.capture_phase is not None:
                core.require(s.capture_generation==s.workers.generation and s.pinned.unchanged(),'recovery_owner')
                core.require(now < s.capture_deadline,'recovery_checkpoint_timeout')
                if s.capture_phase=='quiescing':
                    if 'host' not in s.capture_acks or not self._safe(allow_reviews=True): return
                    if 'native' not in s.capture_sent:
                        s.workers.peers['native'].send({'kind':'capture_pause','token':s.capture_token})
                        s.capture_sent.add('native')
                        return
                    if 'native' not in s.capture_acks: return
                    if not s.capture_fence_sent:
                        s.workers.peers['host'].send({'kind':'capture_fence','token':s.capture_token})
                        s.capture_fence_sent=True
                        return
                    if 'fenced' not in s.capture_acks: return
                    # Both output streams have been drained by Controller.poll;
                    # do not freeze an input whose final frame is partial.
                    if any(p.pending or p.output['stdout'] for p in s.workers.peers.values()): return
                    s.capture_job=CaptureJob(core,s,s.capture_token)
                    s.capture_phase='copying'
                    s.capture_deadline=now+CaptureJob.LIMIT_SECONDS
                if s.capture_phase=='awaiting_owner':
                    status=core.owner.read_json(core.owner.CONTROL/'status.json')
                    if status.get('busy') is False and status.get('ready') is True:
                        if s.capture_paused: self._resume()
                        s.capture_phase=s.capture_token=None;s.capture_acks.clear()
                        s.recovery_refresh=True;s.recovery_next=now+5;s.capture_retry_at=now+5
                    return
                job=s.capture_job
                for record in job.poll():
                    phase=record['phase']
                    if phase=='copied':
                        # Rebase reconciliation additionally needs a quiet live
                        # spool. Regular capture releases admission immediately.
                        if not record['barrier']: self._resume()
                        s.capture_phase='proving'
                    elif phase=='prepared':
                        s.capture_phase='prepared'
                    elif phase=='failed':
                        job.close();s.capture_job=None
                        if record['code'] != 'busy':
                            # Legal pending sends or new exact ledger entries
                            # invalidate only this candidate, never live delivery.
                            if s.capture_paused: self._resume()
                            s.capture_phase=s.capture_token=None;s.capture_acks.clear()
                            s.recovery_refresh=True;s.capture_retry_at=now+30
                            return
                        # No mailbox request has been sent for the busy result.
                        s.capture_phase='awaiting_owner'
                    elif phase=='done':
                        protection=core.wire.recovery_protection({k:record[k] for k in ('floorSeq','epoch')})
                        core.require(record['barrier'] is False,'recovery_reconcile')
                        job.close();s.capture_job=None
                        if s.capture_paused: self._resume()
                        s.workers.peers['host'].send(dict(kind='recovery_protection',**protection))
                        if s.capture_capacity:
                            s.capture_cycles-=1
                            if s.capture_cycles==0:
                                core.require(protection['floorSeq']>s.capture_previous_floor
                                    and record['protectedCount']<4096,'recovery_capacity_requires_review')
                        s.recovery_state='ready'
                        if s.capture_capacity and s.capture_cycles==0: s.recovery_capacity=False
                        s.recovery_refresh=s.recovery_refresh or s.recovery_capacity
                        s.recovery_next=now+core.owner.CHECKPOINT_SECONDS
                        s.capture_phase=s.capture_token=None;s.capture_acks.clear()
                if s.capture_phase=='prepared' and self._safe(allow_reviews=True):
                    job.commit(); s.capture_phase='committing'
                return
            if (not self._safe() or s.recovery_adapter is None
                    or set(s.workers.peers)!={'native','host'} or now<s.capture_retry_at): return
            notification=core.owner.CONTROL/'recovery-refresh.json'
            if notification.exists():
                core.require(core.owner.read_json(notification)=={'kind':'recovery_refresh'},'recovery_notification')
                notification.unlink();s.recovery_refresh=True
            if not (s.recovery_refresh or now>=s.recovery_next): return
            if s.recovery_capacity and not s.capture_cycles:
                with core.recovery_checkpoints.bridge(s.recovery_path) as live:
                    s.capture_previous_floor=live.recovery_coverage()['floorSeq']
                s.capture_cycles=2
            s.capture_token=secrets.token_hex(16);s.capture_phase='quiescing'
            s.capture_capacity=s.recovery_capacity
            s.recovery_refresh=False
            s.capture_generation=s.workers.generation;s.capture_deadline=now+100
            s.capture_acks.clear();s.capture_paused=True
            s.capture_sent={'host'};s.capture_fence_sent=False
            s.workers.peers['host'].send({'kind':'capture_pause','token':s.capture_token})
        except Exception:
            # Keep admission closed until exact job + worker retirement. A
            # broken proof never becomes a retry/ready verdict.
            s.recovery_state='capacity_requires_review' if s.recovery_capacity else 'requires_review'

    def capture_if_safe(self):
        if self.state.settings and hasattr(self.core,'Duplex'):
            return self._async_capture()
        core, s = self.core, self.state
        # Legacy trial-only synchronous wrapper. The formal path above owns
        # explicit worker barriers and an isolated capture process.
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
            if not core.audit_preserved(s.pinned, audit):
                audit.update(state='unknown')
                core.settle_outcome(s.pinned, path, audit, 'media' if media else 'text',
                                    'unconfirmed_quarantined')
            # Recording unknown evidence is not permission to reopen a gate.
            core.require(core.audit_preserved(s.pinned, audit), 'send_requires_review')
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
