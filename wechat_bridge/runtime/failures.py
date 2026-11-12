"""Classify raw failure reasons before diagnostics discard private details."""
# No generic worker_failed/native_worker_failed/host_forward_failed here: those
# also represent config, permissions, evidence and programming errors.
TRANSIENT = frozenset({'worker_exited', 'worker_transport_failed', 'onebot_not_connected',
                       'worker_readiness'})


def transient(code):
    return isinstance(code, str) and code in TRANSIENT


def merge(previous, incoming):
    """A protective reason cannot be replaced by a subsequent generic exit."""
    if previous is None or transient(previous) and not transient(incoming):
        return incoming
    return previous


def exception_code(error, fallback):
    # Do not treat all OSErrors as transport (PermissionError/disk corruption).
    return 'worker_transport_failed' if isinstance(error, (ConnectionError, TimeoutError)) else fallback
