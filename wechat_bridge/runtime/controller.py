"""Single production broker turn and fail-closed recovery orchestration."""
import time
from . import broker
from .failures import merge, transient
from .processes import Workers
from ..adapters.wechat.recovery import RecoveryBackend


class Controller:
    def __init__(self, core, state):
        self.core, self.state = core, state
        self.workers = state.workers = Workers(core, state)
        self.recovery = RecoveryBackend(core, state)

    def _errors(self, name, records, faults):
        for record in records:
            kind = record.get('kind')
            if kind in {'native_error', 'host_error'}:
                self.core.require(kind == name + '_error', 'worker_error_binding')
                # Classification uses the raw value. Only the emitted diagnostic
                # is sanitized; unknown values remain protective.
                raw = record.get('code')
                faults[name] = merge(faults.get(name), raw if isinstance(raw, str) else 'worker_failed')

    def _drain_errors(self, name, capture, faults):
        # Discard ordinary final records after a death: pending output remains
        # conservatively unknown; neither late ACK nor queued event is replayed.
        deadline = time.monotonic() + .1
        for _ in range(4):
            capture.pending.clear()
            try:
                self._errors(name, capture.tick(), faults)
            except (BrokenPipeError, ConnectionError):
                pass
            if time.monotonic() >= deadline or not capture.selector.get_map():
                break

    def poll(self):
        """Collect all peers first so an error/death wins over readiness/data."""
        core, s, workers = self.core, self.state, self.workers
        batches, faults = [], {}
        for name, capture in tuple(workers.peers.items()):
            try:
                records = capture.tick()
            except (BrokenPipeError, ConnectionError):
                records = []
                faults[name] = 'worker_transport_failed'
            self._errors(name, records, faults)
            batches.append((name, capture, records))
            if capture.process.poll() is not None:
                faults[name] = merge(faults.get(name), 'worker_exited')
                self._drain_errors(name, capture, faults)
        core.require(s.pinned.unchanged(), 'native_changed')
        if faults:
            self.restart(faults)
            return True
        for name, capture, records in batches:
            for record in records:
                broker.dispatch(core, s, name, capture, record)
        if ('host' in workers.peers and not s.host_wait['ready']
                and time.monotonic() - s.host_wait['last'] > 12):
            self.restart({'host': 'worker_readiness'})
            return True
        s.voice_relay.check_deadline()
        s.input_relay.check_deadline()
        return False

    def restart(self, faults):
        core, s, workers = self.core, self.state, self.workers
        s.armed = s.sse_connected = s.astrbot_connected = s.good = False
        budget = workers.count(faults)
        # Both old halves must be proven stopped before evidence retirement.
        # Errors are allowed to propagate; there is no catch-and-spawn path.
        workers.stop()
        # A final protective frame can race the initial transport/exit report.
        # Siblings stopped by us do not consume their crash budget, but their
        # evidence errors must still prevent a restart.
        final_faults = dict(faults)
        for name, capture in tuple(workers.peers.items()):
            self._drain_errors(name, capture, final_faults)
        self.recovery.retire()
        workers.close()
        for name, raw in final_faults.items():
            if not transient(raw) or not budget:
                raise core.recovery_coordinator.Failure(core.recovery_diagnostics.code(raw))
        self.recovery.reset()
        workers.next_generation()
        workers.spawn_native()
        for name in faults:
            attempts = workers.supervisors[name].restarted()
            core.recovery_diagnostics.emit('worker', faults[name], counts={'attempts': attempts})
