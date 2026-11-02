# Consistent bridge reboot recovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Review each task before proceeding. The user approved the root-cause design and instructed implementation on 2026-11-02.

**Goal:** Restore the saved WeChat account and bridge after process or host restart while preserving exact unknown-send quarantine, current message/action deduplication, and snapshot-consistent receive boundaries.

**Architecture:** A root-private send disposition ledger supplies one strict restore-admission policy shared by the owner, guest and maintenance. Immutable recovery manifests bind Native snapshots to proven event boundaries and durable bridge dedup coverage. A resumable coordinator selects, verifies, restores and rebinds that state before activating workers; normal bridge restarts reuse the verified live Native instance.

**Tech Stack:** Python 3.12 standard library, SQLite, existing Linux systemd/cgroup/user/network namespace isolation, existing `websockets==15.0.1` transport.

## Global Constraints

- No new QR, initialize, logout, profile deletion, implicit resend, or repeated model invocation to validate recovery.
- Preserve unknown results and exact original Native rows. Queued, running, retryable, mismatched, missing or additional unapproved sends block admission, including WAL records.
- The OneBot worker never receives Native keys, the whole disposition ledger or saved profile.
- Preserve current CPU, memory, process, IPC, queue (32 messages / 2MiB), identity (8192), submission (4096), media and seven-day single-run limits.
- Preserve latest bridge identity, submissions and inbox state across Native rollback. Publishing/uncertain input is never automatically replayed.
- All private files remain owned by the calling owner (root in production), directories 0700, files 0600, symlink/hardlink rejection, bounded reads, atomic replacement and fsync. Errors contain fixed codes, no secrets, real IDs or message bodies.
- Never restart/reconfigure Docker, AstrBot, FRP, host routes/firewall or kernel modules. Operate only exact owned units/PIDs/cgroups.
- Keep v1 legacy migration tickets and their strict tests compatible. New recovery admission is a separate explicit format, not a relaxed v1 exception.
- Implementation worktree: `/home/mukpun/game/.worktrees/wechat-recovery`, base `85ea91b`. Online checkout remains `/home/mukpun/MukPun-WeChat` until reviewed deployment.
- Test Python: `/home/mukpun/MukPun-WeChat/.runtime/verification/onebot-env/bin/python`. Existing dependency-free global Python is insufficient for transport tests. Baseline has the previously documented thumbnail-cancel expectation failure; record and adjudicate it separately, never hide a new failure with a broad skip.
- Physical SATA repair cannot be performed by software. Keep it explicitly outstanding; do not claim the storage fault fixed by application changes.
- User update during implementation: continue, but reduce test rounds and avoid excessive validation. Reuse already recorded evidence. Combine relevant checks once per remaining task; after a fix rerun only affected cases. Reserve the dependency-aware full suite and lifecycle acceptance for one final validation pass. This supersedes repetitive RED/GREEN or broad rerun steps in the task checklists below; preserve meaningful no-resend, quarantine, integrity and recovery coverage.

## Task 1: Immutable send dispositions and strict copied-database admission

**Files:** Create `scripts/send_dispositions.py`, `scripts/restore_admission.py`, `tests/test_send_dispositions.py`, `tests/test_restore_admission.py`. Do not change production callers yet.

**Interfaces produced:**

| Interface | Contract |
| --- | --- |
| `restore_admission.profile_sha256(key_hashes)` | Input exactly `api-token`, `storage-key`, `cursor-key`, each lowercase SHA256. Return canonical lineage SHA256. |
| `restore_admission.fingerprint(account, kind, row)` | `kind` is `text` or `media`. Bind every typed column and account; media stays byte-for-byte compatible with existing `quarantined_send_fingerprint`. |
| `TEXT_COLUMNS`, `MEDIA_COLUMNS` | Text column order: id,idempotency_key,payload_hash,destination,payload,state,client_id,server_id,error_code,created_at. Media order equals existing QUARANTINED_COLUMNS. |
| `restore_admission.pending_records(db)` | Read and return bounded `(kind, row)` records for all nonterminal sends; trusted_schema OFF; caller supplies a private copied DB with journal recovery already performed. Never mutate job/outbox rows. |
| `restore_admission.validate_admission(value)` | Exact format below; return validated value or fixed `ValueError` code. |
| `restore_admission.validate_database(db, account, admission)` | Verify every pending row and require exactly the approved inventory. No mutation. |
| `send_dispositions.entry(account, kind, row, disposition, evidence_kind, evidence_sha256)` | Construct only hashes and fixed classification, no payload/IDs in the stored entry. |
| `send_dispositions.Ledger(path, profile_sha256).read()` | Return schemaVersion/profileSha256/revision/entries. Missing ledger is revision 0/empty. |
| `.record(entry)` | Lock, validate, persist atomically, monotonically increment revision. Identical record is idempotent; conflicting same-fingerprint record rejects. |
| `send_dispositions.admission_for(db, account, ledger)` | Build exact new admission for pending records, reject unmatched/unsafe records; terminal records need no exception. |

Ledger schema: exactly `schemaVersion=1`, `profileSha256`, `revision` (nonnegative int, not bool), `entries`. Maximum 128 entries and 65536 serialized bytes; never drop unresolved entries to make space. Entry fields exactly `kind`, `recordSha256`, `disposition`, `evidenceKind`, `evidenceSha256`. Classifications: `accepted_verified`, `rejected_before_submit`, `unconfirmed_quarantined`, `reviewed_not_delivered`. Evidence pairs: accepted/native_receipt; rejected/native_pre_submit; reviewed/operator_review; unconfirmed/legacy_hold or unconfirmed_result or operator_review.

Admission schema: exactly `schemaVersion=1`, `profileSha256`, `ledgerRevision`, `entries`; maximum 128 entries / 65536 bytes. Only pending records are selected. Entry fingerprints and kinds must be unique. `accepted_verified` alone never admits a pending unknown row.

Media exceptions require `kind=send`, `state=needs_review`, attempts=1, cancel_requested=0, retryable=0 and a complete exact fingerprint. `rejected_before_submit` additionally requires phase preparing/uploading, no server ID or native receipt, and matching pre-submit evidence classification. Quarantined media may be submitting/readback but remains untouched and non-retryable. Text exceptions require `needs_review`, no server ID and a matching explicit unconfirmed/reviewed disposition; queued/submitting text always rejects. Validate shapes, booleans-as-ints, duplicate keys/entries, unexpected columns and bounded inventories fail closed.

- [ ] Write tests with real temporary SQLite and WAL: multiple exact reviewed sends admitted unchanged; extra/changed/queued/running/retryable/WAL pending rows rejected. Test a fresh hash cannot authorize a replayable state. Test lineage/account change and missing approved row. Use synthetic values only.
- [ ] Run the new modules before implementation and capture the expected missing-module failure as RED; then implement the interfaces and rerun to GREEN. Include actual row-byte equality before/after admission.
- [ ] Test private ledger persistence, idempotency, conflicting evidence, capacity refusal, concurrent writers, symlink/hardlink/mode rejection and atomic write interruption retaining the previous valid ledger.
- [ ] Run focused new tests plus `test_native_migration` and `test_media_maintenance_snapshot`; commit task and report exact RED/GREEN commands and outputs.

## Task 2: Immutable manifests and bounded dedup recovery protection

**Files:** Create `scripts/recovery_manifest.py`, `scripts/recovery_snapshot.py`, `tests/test_recovery_manifest.py`, `tests/test_recovery_snapshot.py`; modify `wechat_bridge/durable.py` with tests in `tests/test_recovery_dedup.py`. Do not wire into the live owner yet.

**Interfaces consumed:** Task 1 lineage and typed fingerprints.

**Interfaces produced:**

| Interface | Contract |
| --- | --- |
| `recovery_snapshot.inspect(snapshot)` | Verify existing archive manifest and copied SQLite/WAL; return key hashes, lineage, last canonical event `{cursor,anchor}`, pending inventory and a usable accepted self-proof seed. Never print/serialize raw private content outside root state. |
| `recovery_manifest.Store(directory).publish(snapshot, metadata)` | Copy capped snapshot to immutable content-addressed archive, fsync, write exact bounded manifest, fsync, atomically replace current pointer, fsync directory. Preserve one previous complete generation. |
| `.current()` / `.previous()` | Return only a fully verified manifest and immutable archive path. Reject corrupt pointer, archive hash, modes/links or inconsistent boundary; never silently select uncommitted candidates. |
| `.recovery_plan(current_bridge, manifest)` | Return chosen native boundary and epoch after checking lineage, anchor, inbox certainty and retained dedup coverage; do not restore the bridge DB. |
| `Spool.protect_recovery(floor_seq, epoch)` | Persist protected replay lower bound. It may only advance monotonically within a lineage. Keep latest identities/submissions/inbox. |
| `Spool.recovery_coverage()` | Return retained floor, first sequence, high watermark and epoch; suitable for root proof but no payloads. |

Manifest fields: schemaVersion=1, epoch (32 lowercase hex), profileSha256, snapshotSha256, ledgerRevision, nativeBoundary `{cursor,anchor}`, bridgeBoundary `{cursor,anchor}` or null for bootstrap, dedup `{floorSeq, highSeq}`, selfProof seed (root-private, already verified accepted), createdAt. Both boundaries must exist in the snapshot and bridgeBoundary cannot exceed nativeBoundary. Provide a profile-lineage validation hook if snapshot has no usable event; never invent an anchor. Retain max two committed archives plus one in-progress bounded candidate; interrupted publishing does not replace the previous pointer. Build canonical Native event representation from the pinned SDK source/tests, including timestamps.

Current inbox admits 32 payloads / 2MiB and retains up to 4096 completed metadata. Preserve those caps. While recovery protection is active, prune only completed rows older than the protected floor; if protecting would exceed 4096 completed records, reject before inserting/ACK with fixed `recovery_checkpoint_required` so the owner can refresh the manifest. Retain expired/uncertain source IDs needed for the same recovery window. Old no-manifest callers keep existing behavior. Same ID changed content still fails; same body different ID remains valid.

Inbox certainty means preserving quarantined `uncertain` inputs without replay, as in the existing Spool/Gateway. An `uncertain` row whose identity is retained must not permanently block manifest publication or recovery, including a row older than the protected floor. It remains `uncertain` and is never selected by `next_pending`. Raw `publishing` state must be quarantined by the existing constructor before activation; unknown/malformed states and missing replay coverage still reject. This clarification preserves existing receive behavior after interrupted WS delivery and does not authorize retrying a model event.

- [ ] Write a real two-version archive/SQLite test: snapshot ends at cursor 8, bridge advances to 10, restore plan selects verified 8 while preserving later forwarded and submissions; assert no global bridge rollback.
- [ ] Fault-inject archive copy, manifest write, pointer replace and fsync boundaries, verifying only old or fully new generation is selected; test concurrent publishers.
- [ ] Use >4096 synthetic completed rows to verify retained coverage cannot silently fall behind an available snapshot. Exercise watermark refresh, same ID dedup, changed data and expired/uncertain records.
- [ ] Run focused RED then GREEN, plus `test_session_state_store`, `test_onebot_context`, `test_onebot_routing`; commit and report.

## Task 3: Wire disposition admission through Native restore and sending

**Files:** Modify `scripts/session_runtime_client.py`, `scripts/session_runtime_host.py`, `scripts/session_runtime.py`, `scripts/resume_quarantined_native.py`, `scripts/run_private_forwarder.py`, `scripts/voice_forwarding.py`, `scripts/run_group_probe.py`; create `scripts/recovery_dispositions.py` and integration tests `tests/test_recovery_disposition_integration.py`.

**Interfaces consumed:** Task 1 ledger and admission; Task 2 copied snapshot inspection.

Supporting integration refactor may expose the existing verified copied-database extraction from `recovery_snapshot.py` as a root-only context manager, with its covering tests, if exact audit-to-row matching requires a connection. Preserve inspection, bounds and privacy; never expose raw rows to OneBot.

**Interfaces produced:** `recovery_dispositions.record_native_outcome(core,pinned,audit,kind,classification)` reads a bounded consistent Native inventory and adds an exact disposition; no Native SQL mutation. `recovery_dispositions.import_legacy(core,snapshot,backup_dir)` imports only exact existing holds/human reviews and proven pre-submit failures, retaining root-private evidence and reporting counts. An arbitrary bridge `rejected` label or absent receipt is not proof of non-submission.

Native restore config accepts new optional `restoreAdmission` exclusively with restore=true, and mutually exclusive with legacy `quarantinedSend`. Config reads alone may use a 65536 cap; ordinary mailbox commands stay 4096. Package the pure admission module into SessionHost.MODULES and update exact source allowlists/config size verifiers, including pinned remaining_seconds. Guest hashes restored key files and compares profile before evaluating pending sends. Old v1 ticket/config validation must remain strict and unchanged.

Runtime classifications must be recorded before considering the exception durable. If a snapshot is needed for exact native-row fingerprinting, use the existing owner checkpoint/copy boundary and bounded root API, not mutable live SQL. Unknown result remains unconfirmed/quarantined, not falsely rejected. No matching Native job means no admission exception is necessary, but preserve request dedup and explicit uncertainty evidence. Accepted sends do not need endless exception-ledger entries. At ledger capacity, stop accepting new media/unsafe sends with explicit policy failure while preserving text receive, rather than evicting pending exceptions.

Controller-authorized minimal supporting scope: `wechat_bridge/continuous.py` and the shared text/media receipt contracts recognize fixed `recovery_capacity` only with empty send/job/client/server IDs and no projection confirmation. Return cached OneBot1403 policy rejection; segmented text returns1403 only before any accepted part, retaining1408 for partial/unknown outcomes. Root rejects before forwarding a Native request while receive continues. No queues, retries, receipt loosening or additional POSTs are added. Task3 validation is one combined relevant run; only failing affected tests may be repeated, and the full suite is reserved for Task6.

Only actual pending rows consume the exception ledger. Normal matched terminal history uses an audit-bound marker with admissionRequired=false, copied snapshot/profile hashes and typed row fingerprint. Legacy terminal evidence keeps raw audits intact and uses at most one private fixed sidecar per existing audit name, under backups/CAS/atomic fsync. Non-integer or nonzero retryable media remains pending.

Old hold permission is converted into a stable fingerprint record while preserving raw original audit. Existing reviewed text and media have explicit legacy import checks. New jobs never inherit old permission. Import is idempotent and CAS-bound to copied snapshot/audit bytes, with backups before private state changes.

- [ ] Build a copied snapshot with a historical held image, additional independent reviewed text and pre-submit image. Test new guest restore admits exact inventory unchanged and normal legacy restore still rejects it.
- [ ] Test that startup with an additional newly unknown send, mutated evidence, missing job, profile mismatch or WAL pending send rejects before spawning Native.
- [ ] Test record-before-gate behavior for unknown media and text, lost response, rejected-before-submit and capacity. Ensure no additional POST, send or model call.
- [ ] Test packaging imports and config bounds inside a copied worker-code view. Run RED/GREEN and all existing migration/pipe/voice/runtime tests. Commit and report.

## Task 4: Capture manifests and honor dedup floor in production paths

**Files:** Modify `scripts/session_runtime.py`, `scripts/session_runtime_host.py`, `scripts/forward_host_worker.py`, `wechat_bridge/forward_gateway.py`, `scripts/maintain_media_cache.py`; create `scripts/recovery_checkpoint.py`, `tests/test_recovery_checkpoint.py`, `tests/test_recovery_receive_integration.py`.

**Interfaces consumed:** Task 1 ledger; Task 2 immutable manifest/Spool coverage; Task 3 exact restore inventory and seed.

Supporting production glue may modify the root forwarder, durable spool, fixed wire contract and recovery import glue as required by capture/protection IPC. Preserve all budgets/admission policy and avoid live constructor repair or arbitrary path IPC.

**Interfaces produced:** `recovery_checkpoint.capture(owner_host, bridge_path, state_dir)` selects stable snapshot boundary and atomic manifest; `recovery_checkpoint.rebase_bridge(bridge_path, manifest, expected_generation)` backs up and changes only native receive progress after verifying current dedup coverage and the actual Native anchor. It never rewinds submissions, identities, publishing or uncertain states.

Owner checkpoints publish a recoverable version periodically and after meaningful state transitions. If bridge advances during capture, select a snapshot-contained common boundary and record its proof, not an unverified newer cursor. Anchor verification is against the actual stored canonical event and later the restored API. Copy database with SQLite backup, don't copy raw bridge WAL inconsistently. Snapshot/schema/version change cannot silently invalidate current manifest. Keep two complete generations protected until a newer committed floor is installed in the spool. Avoid lock inversion: recovery coordination lock, then disposition/manifest lock; short SQLite transactions last. Never wait on owner mailbox while holding a bridge SQLite write transaction.

Host worker receives only a small validated dedup floor/epoch, never the root manifest or keys. Activation still happens only after Native restore anchor and authentication succeed. A dedup capacity refresh request is a fixed IPC report to the root owner, not a way to write arbitrary files. If refresh cannot be proved, pause before ACK and surface requires_review/capacity status; don't drop pending messages.

- [ ] Test capture while bridge cursor changes, terminated pointer update and restored event number reuse using real SQLite plus a loopback API. Duplicate same-ID messages never leave the Gateway twice.
- [ ] Test bootstrap and legacy migration common-boundary selection. Missing/expired common event or too-old dedup evidence rejects explicitly; old numeric cursor alone never authorizes recovery.
- [ ] Test owner checkpoints plus maintenance cleanup preserve manifest validity, dispositions and unknown rows.
- [ ] Run focused RED/GREEN, existing durable/Gateway/context/runtime/maintenance tests; commit and report.

## Task 5: Resumable startup coordinator and fixed-code diagnostics

**Files:** Create `scripts/recovery_coordinator.py`, `scripts/recovery_diagnostics.py`, `tests/test_recovery_coordinator.py`, `tests/test_recovery_diagnostics.py`; modify `scripts/run_forwarder.py`, `scripts/run_private_forwarder.py`, `scripts/maintain_media_cache.py`, `scripts/session_runtime.py` and exact owner-identity admission only as needed.

**Interfaces consumed:** Tasks 1-4. Coordinator engine accepts an explicit operations adapter so tests use actual SQLite/archives and synthetic lifecycle/network implementations; production adapter uses existing fixed systemd arguments and pidfd/cgroup validators.

**Engine contract:** `recover(ops)` serializes one attempt and persists stages validate/restore/online/rebind/receive_ready/ready. Before restarting after interrupted attempt, inspect real unit, argv, start ticks, cgroup and attempt marker. Reuse the matching live owner, otherwise verify absence before new restore. Each attempt has a fresh single-use ticket bound to manifest hash, lineage and ledger revision. Preserve consumed history; an observer timeout is not process death. No new entrypoint accepted by owner validators unless exact fixed argument shape is verified.

`run_forwarder.py --serve` coordinates required cold restore before current `image_send_preflight` liveness check; ordinary online restart reuses Native. Maintenance uses the same restore/rebind helper, replacing duplicated special-case startup. Coordinator checks live account GET, uses existing no-QR resume only when needed, then establishes receive boundary and starts workers. Main status distinguishes recovering, awaiting_network, awaiting_user_action, requires_review and ready; known quarantines are nonfatal counts. Retry transient GET/WS/network failures with bounded exponential steps (1,2,4,8,16,30 seconds, max 6 attempts) then a 300-second cooldown; keep durable phase and permit a later safe check, never repeat a send or blindly reauthenticate. Permanent integrity/admission errors remain explicit and do not burn repeated systemd starts.

Keep existing unit limits and exact definition check. Add narrow structured logging of fixed codes/stages/counts only; do not simply enable raw worker stdout. On exited worker, perform bounded drain of final IPC error before generic worker_exited, preserving the most specific public code. No secret messages in log/error arguments.

- [ ] Inject restart after each durable stage; two simultaneous coordinators; existing online owner with stale status; process still alive after observation timeout; one attempt only.
- [ ] Test network unavailable, AstrBot WS unavailable, transient awaiting_approval becoming online and final needs-user-action; no initialize/logout/send and no spurious repeated resume for an already-online account.
- [ ] Replay actual failure combination: empty volatile state, old hold PID missing, exact independently reviewed records, snapshot behind bridge cursor. Require ready with unchanged original quarantined row, preserved newest spool/dedup and no repeated model event.
- [ ] Test real final worker frame followed by EOF preserves `recovery_anchor_mismatch`, fixed structured output excludes injected secret text.
- [ ] Run focused RED/GREEN and existing boot/pipe/health/runtime/maintenance tests; commit and report.

## Task 6: Cross-component lifecycle acceptance, migration and rollout

**Files:** Create `tests/test_recovery_lifecycle.py`, `scripts/migrate_recovery_state.py`, `scripts/accept_reboot_recovery.py`, `docs/reboot-recovery.md`; update `scripts/run_onebot_tests.py`, `tools/sync_runtime.py` packaging as needed, `AGENTS.md`, `README.md`, deployment acceptance records without private data. Add a storage maintenance checklist to docs, not an online hardware mutation.

Migration command supports `check` (read-only) and `apply` (root, backed up, bridge stopped, all evidence rechecked). Back up source snapshot, consistent bridge DB, audit/hold/ledger/tickets before changing only owned recovery files/receive metadata; preserve original files. Legacy import and common-boundary selection use production policy, not ad-hoc SQL terminal rewrites. Apply is idempotent and interruption-safe. No full host reboot during deployment.

Acceptance runs the real coordinator and admission/manifest code over two genuine isolated process lifecycles and local synthetic Native/OneBot APIs. Use empty volatile state between lifecycles, controlled worker terminations and immutable copied fixtures; no actual account, real message send or model invocation. Include both cold restore and online owner reuse. Preserve existing cgroup/read-only/zero-caps/private-network resource checks and compare host/Docker/FRP baseline. Exercise all eight acceptance requirements in the approved design, including >4096 metadata pressure and uncertain WS outcomes.

- [ ] Start with failing lifecycle tests against the old boot semantics; cover exact historical inventory and cursor reuse, all durability fault boundaries and replayable extra task rejection. Real subprocess/API behavior is required beyond mocked calls.
- [ ] Verify dependency-aware full suite and OneBot simulation. If the pre-existing thumbnail-cancel test is stale against the documented original-image policy, fix its expectation only with a focused justified regression test; if an implementation bug, isolate the minimal fix and review it explicitly. Do not skip it to claim green.
- [ ] Run source-only publication audit and review new/changed files for credentials/private records. Check exact packaging includes new root/guest modules and excludes private root ledger from OneBot.
- [ ] Perform whole-branch independent review and fix Critical/Important findings; rerun affected tests and re-review.
- [ ] Prepare concrete root-private rollout with current baseline, safe pause, full backups, reviewed source install and runtime-view sync. Read-only migration check first; apply only after predicates pass. Start only this project's bridge, reuse current live account and verify Native/SSE/WS/queue/latency/natural messages with no extra outbound tests. Keep rollback to previous code plus original recovery metadata; never roll back current live message state wholesale.
- [ ] Record actual test counts, isolated lifecycle evidence, online health and remaining physical SATA repair. Commit final docs/tests. Do not label a mock-only run as real host reboot or claim hardware repair.

## Controller bookkeeping and review

For each task record base/head commits, RED/GREEN evidence, task report, review verdict and all remaining issues in `.superpowers/sdd/progress.md` (local ignored workflow state). Use task-specific diff packages; one implementer at a time. Review APIs/callers before marking each task complete. User authorization covers this plan's implementation and necessary project-only deployment; no extra confirmation between tasks.
