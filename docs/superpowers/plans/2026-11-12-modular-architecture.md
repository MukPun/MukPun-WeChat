# MukPun-WeChat Modular Architecture Implementation Plan

> For agentic workers: use subagent-driven-development or executing-plans task by task. Read the design and this task's brief, update the tracked progress record after review.

**Goal:** 在原有进程和功能边界内形成公共内核、微信适配器、OneBot 动作层、网络层、存储与运行管理。
**Architecture:** 逐步切换正式调用路径，旧模块保留兼容导出；各阶段可独立测试，不同时启动两个真实消费者。
**Tech Stack:** Python 3.12；保持现有固定依赖。

## Global Constraints
- Preserve current dependencies, SDK pins, resource limits, public actions/echo/error shapes, routing and media behavior.
- Preserve SQLite schemas, IDs, serialized state, admission ordering, deduplication and send evidence.
- Unknown sends and recovery evidence conflicts never auto-replay, delete audit, skip cursor, or trigger fresh login.
- Keep current process/credential separation. No live service, host-network, Docker, AstrBot or FRP changes; no real messages or model calls.
- Every task updates DEVELOPMENT.md, the design and docs/architecture-rewrite-progress.md with implementation, NapCat reference, tests and remaining work.
- Existing baseline full suite was interrupted by host journal I/O; record incomplete honestly. Use focused synthetic tests per task and one final full run.
- No real QQ backend, new OneBot APIs/transports, WebUI, SDK upgrade, schema migration or background snapshot concurrency.

## Task 1: Package delivery, shared contracts and storage
Files: tools/sync_runtime.py; packaging/isolation manifests in scripts; new wechat_bridge/core and storage; state.py/durable.py compatibility exports; layout/storage/architecture tests.
- [ ] Read NapCat Core and its network adapter interface.
- [ ] Add failure-first tests proving nested packages are synchronized, stale managed source is removed without removing venv/data, and symlink traversal/excluded staging/private assets are rejected. Keep the existing root test runner alias.
- [ ] Replace top-level-only copying with a deterministic explicit public-code/role manifest shared by synchronization and worker verification. Support package __init__.py and nested code without accepting staging or private runtime files. Preserve current isolation allowlists; no blanket repository mounts or credential access.
- [ ] Add small platform-neutral contracts for conversation identity/kind, messages, send result/receipt, capabilities/health and the platform backend/policy interfaces, reusing current representations where possible; no unused generalized framework.
- [ ] Move existing State/Spool implementation into storage with old imports re-exported. Keep schemas, exception/class identity, constructor behavior and data formats compatible.
- [ ] Prove old DB fixtures, publishing retirement, exact ID/echo behavior and existing layout tests; add dependency boundary checks used in subsequent tasks.
- [ ] Commit, report RED/GREEN commands and update design/development/progress.

## Task 2: WeChat adapter and Native entrypoints
Files: new wechat_bridge/adapters/wechat; forward_native_worker.py, forward_sse.py, forward_gateway.py and existing Native receive/send/media helpers.
- [ ] Read NapCat Core/API/event wrapper; note differences from HTTP/SSE and durable receipts.
- [ ] Write contract tests for normalized group/private sources, opaque checkpoint+ACK order, own-message filtering, complete text/image/voice sends and known-rejected versus unknown outcomes.
- [ ] Consolidate SDK HTTP/SSE, raw identity/group rules, message projection and media/receipt operations under WeChat adapter modules. Existing low-level implementations may be moved or delegated to rather than reimplemented.
- [ ] Split Gateway into a WeChat IPC backend proxy and platform-neutral durable ingestion/delivery responsibilities. Preserve request/chunk binding and the existing IPC frame limits/serialized payloads.
- [ ] Native worker becomes an isolation/handshake/adapter assembly entrypoint. Provide a host-side proxy implementing the same backend contract without holding Native credentials.
- [ ] Formal production path uses adapter interfaces. Existing script imports remain compatible and role manifests include required package dependencies.
- [ ] Run Native/SSE/routing/media/IPC focused suites, document contracts/reference, commit and report.

## Task 3: OneBot service, action registry and message delivery
Files: new wechat_bridge/onebot and core message/delivery modules; protocol.py and continuous.py compatibility layers.
- [ ] Read NapCat action/index.ts, OneBotAction.ts and OneBot event conversion.
- [ ] Write failure-first tests for registry dispatch, full request validation, opaque echo, unsupported actions/segments, group/private ambiguity and fake backend operation with non-WeChat IDs.
- [ ] Extract JSON/OneBot decoding and response/event construction into onebot. Register independent action handlers grouped by send, status/capability and directory queries; retain exact supported subset and response codes.
- [ ] Extract routing/persistent ingestion/text splitting/delivery policy out of protocol inheritance into composed services. Inject platform policy; public core must not inspect @chatroom or import concrete WeChat modules.
- [ ] Keep existing PrivateBridge/TextBridge/Bridge import and constructor compatibility for existing consumers; formal host composition must use the new service/registry, not a parallel old engine.
- [ ] Preserve existing state/response serialization and behavior for mentions, group context, names, links, media, reply windows, partial sends and idempotency.
- [ ] Run focused OneBot/state/roundtrip suites, update docs/reference decisions, commit and report.

## Task 4: Transport composition and shared health
Files: new wechat_bridge/transports; transport.py compatibility exports; forward_host_worker.py, forward_wire.py, native_health_probe.py; core health and configuration assembly.
- [ ] Read NapCat NetworkManager, websocket client, config reload and GetStatus.
- [ ] Write failure-first tests for transport-service isolation and the observed Native-offline/OneBot-online inconsistency, stale and failed probes, reconnect and request decoding.
- [ ] Transport owns connection I/O only and calls a protocol service/event source; keep loopback restriction, headers, action/frame limits, heartbeat and current reconnection budgets. Only reverse WS is implemented.
- [ ] Assemble existing config into internal views and inject endpoint/clock/backend/store at entrypoints, keeping current public config and endpoint policy.
- [ ] Introduce a bounded validated health IPC frame from controller to host. Protocol get_status and heartbeats read the same fresh Native/SSE/WS/recovery health record as controller status. Legacy fixture backends remain testable through explicit composition.
- [ ] Update both IPC ends and role manifests together; mixed worker code is rejected by existing version/digest admission. Preserve existing status fields; additions are nonsecret.
- [ ] Run transport/health/config/action/roundtrip tests, update docs/reference, commit and report.

## Task 5: Runtime lifecycle, error classification and safe worker recovery
Files: new wechat_bridge/runtime and WeChat recovery backend; run_private_forwarder.py, run_forwarder.py, worker_supervisor.py, recovery* and voice_forwarding.py as needed.
- [ ] Read NapCat shell/napcat.ts; write integration tests for controller behavior, not just crash counter helpers.
- [ ] Extract worker process ownership/launch, session handshake/state, IPC broker/media handling and recovery coordination into bounded modules. Keep the exact formal CLI/unit names and Native session pinning.
- [ ] Unify error frames and process exit per worker generation. Classify transient transport/process failures for safe recovery and identity/evidence/config/permission failures for existing blocking policy; unknown outcomes remain protected. Count once; intentional sibling shutdown does not consume its budget.
- [ ] Restart both handshake halves only after old units are verified stopped and pending output disposition and publishing retirement persist. Failure aborts recovery; never swallow and spawn anyway.
- [ ] Recreate per-session image/voice/input/request/ACK state; preserve Native session and expensive admission. Reconnect using admitted durable checkpoints, never latest. Keep 10-second/third-crash budget and original safe-point snapshot protocol.
- [ ] Connect health publication and readiness to real Native/SSE/WS/admission state. Do not report ready early.
- [ ] Run crash/error/handshake/in-flight image+voice/unknown/audit-failure/recovery integration suites, update docs/reference, commit and report.

## Task 6: Integration, handoff and acceptance
Files: source/role packaging, existing simulation and isolated acceptance runners; DEVELOPMENT.md, design, AGENTS.md, progress record and relevant topic docs.
- [ ] Verify dependency boundaries: core imports no concrete SDK, OneBot network or systemd; transport imports no SQLite/SDK; host cannot obtain Native secrets; formal paths resolve new services.
- [ ] Run full unittest once after focused suites pass; investigate actual failures, do not relabel an I/O-interrupted run as passing. Run simulation and publication audit. Run isolated Linux acceptance when available, documenting privilege/environment limitations.
- [ ] Verify old DB/state fixtures and clean code-view imports; add new worker/runtime suites to restricted acceptance runner where existing discovery would omit them.
- [ ] Request independent whole-change review and fix important findings with focused evidence.
- [ ] Complete development/design/AI navigation and tracked handoff with commit IDs, commands/results, deployment-not-done, known issues and exact next steps. Leave worktree reviewable; no production deployment or push/merge implied.
