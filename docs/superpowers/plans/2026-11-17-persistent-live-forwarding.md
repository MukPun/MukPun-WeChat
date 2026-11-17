# Persistent live forwarding implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement and review these tasks sequentially. User approved implementation on 2026-11-17; continue without repeating design approval.

**Goal:** Persist Native session data directly, ignore pre-start input backlog, and remove ordinary snapshot failures from healthy forwarding's stop conditions.

**Architecture:** A root-controlled persistent Native data directory is bound only into the exact Native guest after private-directory and ownership checks. Formal live startup reuses the existing account and establishes a fresh receive boundary without restoring old inbound work. Automatic five-minute archive/capture work is disabled for this mode; preserved legacy archives remain migration/rollback evidence. Optional backups run outside forwarding and never acquire authority to stop healthy bridge or Native processes.

**Tech Stack:** Python 3.12, SQLite, existing systemd namespace/IPC/OneBot architecture; no new dependencies.

## Global constraints

- Private chats remain bound; allGroupsEnabled=true/privateOnly=false and five media capabilities remain as authorized.
- No fresh login, logout, QR reset, message replay, proactive test message or additional model call.
- Preserve existing identity mappings, action idempotency, 54 historical unresolved actions, five Native dispositions, ledger revision 5, audits and recovery archives.
- CPU/memory/task/swap/individual-file limits and other-service/network isolation remain. Persistent disk data needs explicit inventory/headroom enforcement; do not claim tmpfs provides a disk quota after migration.
- A persistent data mount must never be exposed to OneBot. Validate no symlinks/hardlinks or concurrent Native writers; preserve full keys/profile/SQLite sidecars.
- All new modules/tests go into explicit runtime_manifest/guest dependencies and restricted runners.
- Reuse existing worktree codex/wechat-private-canary. Use only targeted tests for actual changes and meaningful Linux isolation checks; no repeated full suite.
- Only migrate production after code review, isolated verification and a fresh, identity-bound rollback copy. Operate exact project units only.

### Task 1: Native persistent data lifecycle

**Files:** new scripts/session_persistent_store.py and tests/test_session_persistent_store.py; modify scripts/session_runtime_host.py, scripts/session_runtime_client.py, scripts/session_runtime.py, scripts/run_group_probe.py and their focused tests. Register source/tests in scripts/runtime_manifest.py and relevant runner.

**Interfaces:** persistent mode is explicitly enabled by root-private state, not by presence of arbitrary files. The guest receives a storage-mode flag, never host private paths. The root store owns an exclusive lifetime lock and validates source inode, full inventory, profile and UID handoff before Native may run. Existing snapshot-only/offline fixtures stay supported. Export a small root-side mode/status accessor usable by formal startup; document its exact signature in the task report.

- [ ] Add failing tests for data surviving two genuine directory/SQLite lifecycles, exclusive writer lock, UID handoff/sealing, bad paths/objects rejected, and no archive prerequisite once persistent mode is enabled.
- [ ] Implement no-follow root-private directory handling and safe explicit initialization from a verified stopped-source or archive; use durable publish of storage metadata after complete validation.
- [ ] Bind persistent data only into Native; retain tmpfs scratch isolation, verify guest mount/source identity, pin the exact UID/PID before access, and preserve startup outbox admission before any Native autowork.
- [ ] In persistent mode disable automatic five-minute and mailbox-triggered archive saves; normal shutdown is not contingent on backup success. Legacy mode remains available for old recovery paths.
- [ ] Verify inventory/headroom and record storage-mode/backup status using fixed codes, no private values. Add manifest/guest dependencies and run relevant focused tests.
- [ ] Report changed files, RED/GREEN command/output, interfaces, resource and migration limitations. Do not operate production.

### Task 2: Live-only bridge startup and receive boundary

**Files:** new scripts/live_forwarding_startup.py if needed; scripts/run_private_forwarder.py; wechat_bridge/runtime/session.py, workers.py/state.py; wechat_bridge/adapters/wechat/native.py/recovery.py; scripts/forward_host_worker.py; wechat_bridge/storage/spool.py or durable implementation; focused startup/queue/worker tests; explicit delivery inventory.

**Interfaces:** task 1 storage mode accessor selects formal live startup. Self proof remains bound to the account and GET-verified before receive. Startup supplies one exact fresh cursor+anchor, persisted before activation. Only the full bridge start discards historical queued input; worker respawns/WS reconnects do not reset an established run's boundary.

- [ ] Add failing tests that old queued and unconsumed Native input are not published after a full start; historical publishing/uncertain and action evidence remain; post-boundary new input publishes once.
- [ ] Bootstrap from authenticated Native current boundary with real anchor, without the old selected recovery snapshot as a delivery prerequisite. Verify account/self identity using durable preserved proof.
- [ ] Persist skipped input state/count and fresh boundary atomically in the host before ready; disallow activation before boundary proof. Preserve no-gap subscription and same-run reconnect behavior.
- [ ] Bypass periodic recovery capture/protected-floor refresh for this live-only mode, retaining bounded inbox pruning and idempotency evidence. Identity/current storage/protocol corruption remains distinct from a backup failure.
- [ ] Register modules and run meaningful affected topic tests. Report exact interfaces and results; no production operations.

### Task 3: Backup failure boundary and integrated review

**Files:** scripts/session_runtime.py/session_runtime_host.py; adapter recovery boundary; backup diagnostic helper only if needed; tests/test_persistent_live_lifecycle.py and existing focused suites; docs/DEVELOPMENT.md, docs/reboot-recovery.md, architecture handoff/progress and AGENTS.md.

- [ ] Prove persistent live mode starts and forwards synthetic messages when archive writes raise OSError or old capture workers fail/are unavailable; no periodic freezer/capture is invoked.
- [ ] Exercise a synthetic two-process restart with real SQLite and fail backup, verifying identity/unknown-send protection, skipped backlog and fresh messages once. Check timeout/crash/writer-lock and stop cleanup, using project Linux restricted runner when applicable.
- [ ] Review the full change and fix concrete findings; audit publication and source-role contracts. Record which old paths stay legacy-only, storage growth bounds and offline backup policy.
- [ ] Commit only reviewed public code/docs. Do not imply a commit means deployed or push unless requested.

### Task 4: Controlled production migration and current health

**Files:** a narrow reviewed one-shot operation and exact rollback evidence in root-private state, no credentials committed.

- [ ] Read-only refresh: bridge/owner PID+ticks/account, private config/capabilities, GET self proof/receive tail, inbox/action/audit/ledger counts, unit/cgroup/host baselines and deployed source view.
- [ ] Prepare and verify a consistent full session rollback copy and candidate persistent directory under locks; preserve original failure/completion/journal bytes. No old one-shot tool reuse.
- [ ] Stop only the exact owner/Native for migration after candidate is durable, prove old Native writer and workers retired, transfer verified full profile into persistent storage, then start from the saved account only. Bridge backlog is skipped under newly implemented policy, never replayed.
- [ ] Update the precise project startup/guard to persistent live mode, synchronize explicit source view, start bridge and verify fresh ready/Native/SSE/WS/identity, disk persistence, no active periodic capture, preserved old actions/ledger and unchanged other services.
- [ ] Observe existing normal traffic only; no extra test sends/model calls. Record actual success/failure and limitations in the plan/handoff, with exact evidence, preserving rollback on any failed prerequisite.

## Progress

- Initial state: diagnosis/design documents modified locally; no implementation, migration, Native restart or bridge recovery completed.
