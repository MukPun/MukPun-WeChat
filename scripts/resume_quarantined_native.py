#!/usr/bin/env python3
"""One explicitly approved Native v1.2.0 restore; never initialize or send.

The ordinary session entry point does not read this one-use, root-private ticket.
A failed migration requires review; this entry never retries or replaces state.
"""
import hashlib
try:
    import fcntl
except ImportError:
    fcntl = None
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import stat
import sys


def load(name):
    spec = importlib.util.spec_from_file_location("_migration_" + name,
                                                 Path(__file__).with_name(name + ".py"))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


owner = load("session_runtime")
guest = load("session_runtime_client")


@contextmanager
def migration_lock():
    if fcntl is None:
        raise ValueError("linux_required")
    descriptor = os.open(owner.STATE / "native-migration.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("migration_lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def consume_ticket():
    with migration_lock():
        return _consume_ticket()["quarantinedSend"]


def _consume_ticket():
    ticket_path = owner.STATE / "native-1.2.0-migration.json"
    ticket = owner.read_json(ticket_path)
    if (not isinstance(ticket, dict)
            or set(ticket) != {"schemaVersion", "phase", "snapshotSha256", "quarantinedSend"}
            or type(ticket["schemaVersion"]) is not int or ticket["schemaVersion"] != 1
            or ticket["phase"] != "prepared"):
        raise ValueError("migration_ticket")
    guest.validate_quarantined_send(ticket["quarantinedSend"])
    snapshot = owner.STATE / "checkpoints/private/current.zip"
    descriptor = os.open(snapshot, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or not 0 < info.st_size <= guest.state_store.MAX_ARCHIVE_BYTES):
            raise ValueError("migration_snapshot")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if ticket["snapshotSha256"] != digest:
            raise ValueError("migration_snapshot")
    finally:
        os.close(descriptor)
    # Consuming before startup makes an interrupted attempt fail closed.
    owner.write_json(ticket_path, {**ticket, "phase": "consumed"})
    return ticket


def main():
    if os.geteuid() != 0 or sys.argv[1:] != ["--serve"]:
        return 2
    with migration_lock():
        ticket = _consume_ticket()
        return owner.serve(False, seconds=owner.LONG_RUNTIME_SECONDS,
                           quarantined_send=ticket["quarantinedSend"],
                           restore_sha256=ticket["snapshotSha256"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        owner.wire({"stage": "native_migration_result", "ok": False,
                    "code": "migration_requires_review"})
        raise SystemExit(1)
