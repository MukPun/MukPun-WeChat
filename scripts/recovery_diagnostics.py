"""Bounded, fixed-code recovery diagnostics. Never print worker output."""
import json
import sys
import time

STAGES = frozenset({'validate','restore','online','rebind','receive_ready','ready','recovery','worker'})
CODES = frozenset({'ok','recovering','awaiting_network','awaiting_user_action','requires_review',
    'recovery_admission','recovery_integrity','recovery_owner','recovery_anchor_mismatch',
    'recovery_rebase','recovery_reconcile','self_identity_unverified','worker_exited',
    'worker_failed','native_worker_failed','host_worker_failed','onebot_not_connected',
    'sse_error','invalid_recovery_anchor','recovery_checkpoint_required'})
COUNTS = frozenset({'quarantined','uncertain','verified','attempts'})


def code(value, default='worker_failed'):
    return value if isinstance(value,str) and value in CODES else default


def emit(stage, value, *, counts=None, stream=None):
    record={'stage':stage if stage in STAGES else 'recovery','code':code(value,'requires_review')}
    record.update({key:number for key,number in (counts or {}).items()
                   if key in COUNTS and type(number) is int and 0 <= number <= 8192})
    print(json.dumps(record,separators=(',',':'),sort_keys=True),file=stream or sys.stderr,flush=True)


def exited_code(capture):
    """Drain at most two bounded frames/100ms after exit, preferring final error."""
    deadline=time.monotonic()+.1
    for _ in range(4):
        # tick can also write an outstanding input to a closed worker pipe.
        capture.pending.clear()
        try:
            records=capture.tick()
        except (BrokenPipeError,OSError):
            records=[]
        for record in records:
            if record.get('kind') in {'native_error','host_error'}:
                return code(record.get('code'))
        if time.monotonic()>=deadline or not capture.selector.get_map():
            break
    return 'worker_exited'
