"""Persistent-account live startup; old snapshots never authorize delivery."""
import re
import time

_resumed_owners = set()


def enabled(core):
    return core.owner.companion('session_persistent_store').enabled(core.owner.STATE)


def read_binding(core):
    value = core.owner.read_json(core.owner.STATE / 'live-forwarding.json')
    core.require(isinstance(value, dict) and set(value) == {'schemaVersion', 'profileSha256', 'selfProof'}
        and type(value['schemaVersion']) is int and value['schemaVersion'] == 1
        and isinstance(value['profileSha256'], str)
        and re.fullmatch('[0-9a-f]{64}', value['profileSha256']), 'self_identity_unverified')
    core.loops.proof(value['selfProof'])
    storage = core.owner.companion('session_persistent_store')
    core.require(storage.profile_fingerprint(core.owner.STATE) == value['profileSha256'],
                 'self_identity_unverified')
    return value


def startup(core):
    core.require(enabled(core), 'saved_session_missing')
    read_binding(core)
    if not core.owner.owner_active():
        # Persistent startup itself refuses missing data or unreviewed outbox;
        # it never initializes a replacement account or loads an old archive.
        core.owner.start_owner(False, seconds=core.owner.LONG_RUNTIME_SECONDS)
    value = core.trial.fresh_status()
    core.require(value.get('accounts') == 1, 'saved_session_not_ready')
    if value.get('connected') is not True:
        identity = core.tap.details(core.owner.OWNER).get('MainPID')
        core.require(isinstance(identity, str) and identity.isdecimal() and int(identity) > 1,
                     'saved_session_not_ready')
        if identity not in _resumed_owners:
            _resumed_owners.add(identity)
            core.owner.client('resume')  # Existing account only; this API cannot initialize/QR.
            value = core.trial.fresh_status()
        if value.get('connected') is not True:
            return {'status': 'awaiting_network', 'phase': 'online', 'retryAt': time.time() + 5}
    # Actual self-sender GET proof, SSE and WS still precede activation in the
    # existing worker handshake. No stale recovery journal is rewritten here.
    return {'status': 'recovering', 'phase': 'receive_ready'}


def mark_ready(core):
    core.owner.write_json(core.owner.STATE / 'live-forwarding-status.json',
        {'schemaVersion': 1, 'status': 'ready', 'phase': 'ready', 'updated': int(time.time()),
         'receivePolicy': 'live_only', 'backup': 'disabled'})


def block(core, code):
    safe = code if isinstance(code, str) and re.fullmatch('[a-z][a-z0-9_]{0,63}', code) else 'requires_review'
    core.owner.write_json(core.owner.STATE / 'live-forwarding-status.json',
        {'schemaVersion': 1, 'status': 'requires_review', 'phase': 'blocked',
         'updated': int(time.time()), 'code': safe, 'receivePolicy': 'live_only', 'backup': 'disabled'})
