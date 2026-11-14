# 架构重构接力记录

## 固定上下文
- 源码基线：5ce68e554f4de309d8474d9176ccfa6953fb6048。
- 工作区：本文件所在仓库；主仓库已有未跟踪暂存目录，不移动、不清理。
- 设计：[模块化设计](superpowers/specs/2026-11-12-modular-architecture-design.md)。
- 任务：[实施计划](superpowers/plans/2026-11-12-modular-architecture.md)。
- NapCat 本地参考：/home/mukpun/game/.references/NapCatQQ；提交 72912f293924f43463b5976b01fc094a8ae11481。
- 用户批准：分阶段替换，兼容现有能力；每阶段代码、开发/设计文档、交接记录同步。

## 验证基线
早期主仓库全量因 jbd2_log_wait_commit 长等待中断（130），该基线没有完整通过结论。随后 clean worktree 在 `0452ba7` 完成 1232 tests / 72.006s / OK，见 Task 5 复审修复记录。Task 6 沿用该证据，仅补实际修正的定向验证，不重复全量；未部署或修改生产服务。

## 任务进度
| Task | 内容 | 状态 | 提交/复审/验证 |
|---|---|---|---|
| 1 | 子包交付、公共契约、存储 | complete | `d460163`、`9f67d4b`；独立审查 compliant / approved，无发现；163 tests 通过 |
| 2 | 微信适配器 | complete | `965699b`；独立审查 compliant / approved；249 项专题及兼容检查通过 |
| 3 | OneBot 注册和消息服务 | complete | `47e4a9d`、`44cf087`；独立审查 compliant / approved；264 tests 通过 |
| 4 | 网络、配置、健康 | complete | `f1aeb71`；独立审查 compliant / approved；329 专题及 67 修订后验证通过 |
| 5 | 控制器与恢复 | complete | `27a0fa4`、`0452ba7`、`de80b40`；独立复审通过；1232 项全量通过 |
| 6 | 集成、复审、交接 | complete（源码/文档） | `eb0c666`、`f8dfa42`、`c8ba744`；整体静态复审通过且无剩余发现；isolated 待 sudo 权限 |

## 当前接手结论
Tasks 1–5 已完成并复审通过，正式路径与兼容入口见[当前架构及 AI 交接](modular-architecture-handoff.md)。本轮未部署、未发真实消息、未调用模型、未改变现有服务/网络。下一步只做整体静态复审及其真实发现，另需有权限的 systemd 合成验收窗口。下列 Task 1–5 是按时间保留的历史实施证据，其“待后续任务/待复审/待全量”不能覆盖当前表格。

## 接力规则
每个实施任务在本表更新状态，附具体提交、RED/GREEN 命令和结果、NapCat 对应实现/差异及未完成事项。先看本表和 Git，再继续第一个未完成任务，不重复已完成任务。不能把计划中的模块写成已实现。
生产切换未授权，本轮不部署、不发真实消息、不调用模型。临时评审包位于 .superpowers/sdd；该目录不提交。


## Task 1 实施记录（2026-11-12）

基于批准计划提交 `ead487e27210f6e609edf987c1044125624cfb46`，在独立 detached worktree 实施；实现提交：`d460163394588bdf63d34d87dcc75569ddaf0810`。状态为已实现、待独立复审，不表示整体架构完成。

交付文件：新增 `scripts/runtime_manifest.py` 显式公开源码/角色清单；同步工具支持包、旧管理代码清理和路径边界；控制器与 Native 检查共享角色清单，host 增加包存在/类型校验。新增 core 小型契约；存储实现移入 storage，旧导出保持类/异常同一对象及持久行为。补布局、架构、旧库与干净视图导入测试；受限 runner 加入架构和 publishing retirement 专题；发布审计跳过忽略的 `.superpowers` 草稿。

NapCat：已读 `packages/napcat-core/index.ts` 和 `packages/napcat-onebot/network/adapter.ts`（上述固定提交）。采用共享能力边界与协议/网络生命周期分工；避免其将 Core/session 引用传入网络适配器的做法，继续用受限 IPC 和不带凭据的 Protocol。当前 Native 保留 flat `/opt`，adapter 子包角色映射在 Task 2 扩展。Task 4 再接入网络生命周期/健康，不在本阶段建立另一个消费者。

RED（实现前）：`python3.12 -m unittest tests.test_project_layout tests.test_architecture_foundation -q`，14 tests，2 failures + 11 errors；缺失 core/storage/manifest API，且 `.superpowers` 被纳入审计。随后新增 runner 覆盖断言单测 RED：1 test，1 failure（受限 runner 未收录架构测试）。对应原始日志位于忽略草稿 `.superpowers/sdd/task-1-report.md`，没有把预期失败伪写成通过。

GREEN（合成，无真实凭据/发送/模型）：

```bash
TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B -m unittest tests.test_project_layout tests.test_architecture_foundation tests.test_durable_quarantine tests.test_onebot_bridge tests.test_onebot_continuous tests.test_onebot_routing tests.test_recovery_dedup -q
TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B -m unittest tests.test_onebot_native_forward tests.test_onebot_forwarder_pipe tests.test_onebot_action_transport tests.test_recovery_migration -q
python3.12 -B tools/audit_publication.py .
git diff --check
```

第一组最终通过 87 tests（8.790s）；第二组通过 76 tests（23.054s），合计 163 tests，输出含一次 asyncio 慢任务诊断，无测试失败。发布审计通过：290 files、findings=[]；git diff --check 无输出、退出 0。实现比对确认 storage 代码与基线逐字相同（仅 sender_names 相对导入深度调整）。干净代码视图验证实际导入 host 包和 flat Native worker，未启动服务。tmpfs 只用于测试夹具，生产同步写入设置未改变；该验证不证明物理磁盘故障持久性。

控制器另在原始 main 副本使用同一 tmpfs 环境完成基线：1150 tests in 67.789s，FAILED(errors=1)，无 assertion failure。唯一错误是 `test_recovery_disposition_integration.PackagingTests.test_actual_isolated_paths_in_copied_worker_view` 对原始 scripts 做 copytree 时遇到未跟踪、不可读取的 `scripts/context-staging`。本独立工作副本没有该私有 staging；该原始工作区环境错误不是本改动的源码回归。干净工作副本的最终全量仍由 Task 6 统一执行。本阶段没有重跑全量或 systemd 隔离验收，没有部署或生产操作。

复审关注：显式 inventory 与 Native 角色源映射是否完整；之后新增包必须主动登记。sync 仅删除旧清单登记的公开代码，未登记的旧文件留存，不能将它当作私有目录清理器。后续 adapter、OneBot registry、transport/runtime 正式行为尚未实施。独立 reviewer 检查完本任务后再进入 Task 2。

独立 Task 1 审查结论：spec compliant、quality approved，无 Critical/Important/Minor 发现；已核对完整交付清单、Native 依赖、存储逐字兼容和隔离入口。Task 1 完成，下一项为 Task 2。


## Task 2 实施记录（2026-11-12，历史记录；现已独立复审通过）

基于 `422d83c39e99f7dd1b370fd0c70853b282191f49`，新增 `adapters.wechat` 的 Native/SSE、消息投影、来源/checkpoint 策略、无凭据 IPC proxy 和三态证据投影；提取 `core.delivery.DurableInbox`。正式 host 导入新 proxy；旧 Gateway/Native/SSE/continuous 导出兼容。Native flat 角色映射新增两个包源码目标，所有公开文件显式登记；新 `test_wechat_adapter.py` 已进入受限 runner。没有改 schema、SDK、帧/媒体/资源限额或审计恢复规则。

NapCat：本地固定提交 `72912f293924f43463b5976b01fc094a8ae11481`，读 `packages/napcat-core/index.ts`、`apis/msg.ts`、`helper/event.ts`。采用 API 聚合、委托原生客户端、异步完成绑定；微信保持 HTTP/SSE 的持久 ACK、锚点和回执/消息投影证明，不能照搬 QQ listener 超时语义。不使用远端重读或 QQ 代码。

RED：`tests.test_wechat_adapter` 在实现前运行 6 tests，6 failures（缺失公共 adapter）；受限 runner 登记单测另有 1 test / 1 failure。迁移后首轮 51 tests 通过；随后媒体/Native 组合 164 tests 有 27 failures（checkout 在 `-I` 下的 SSE wrapper 误只找 flat 文件，worker 在握手前退出），通过直接 `-I` import 的堆栈定位后补 checkout 源路径回退；没有修改服务状态或放宽隔离。

最终 GREEN，均使用 `TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B -m unittest`：

```text
tests.test_onebot_native_forward -q
  Ran 35 tests in 15.168s; OK
tests.test_wechat_adapter tests.test_architecture_foundation tests.test_onebot_routing tests.test_onebot_continuous_wire tests.test_onebot_loop_guard tests.test_text_projection_wait tests.test_onebot_context tests.test_onebot_sender_names tests.test_hd_gateway -q
  Ran 75 tests in 9.225s; OK
tests.test_onebot_forwarder_pipe tests.test_onebot_image_output tests.test_onebot_image_output_native tests.test_onebot_voice_native tests.test_onebot_voice_output tests.test_onebot_output_policy tests.test_onebot_all_groups tests.test_onebot_mentions tests.test_onebot_images tests.test_project_layout -q
  Ran 139 tests in 2.424s; OK
```

合计 249 focused tests；输出有两条 asyncio 慢任务诊断，无测试失败。新 public adapter 用真实 Spool 核对 commit/checkpoint 先于 ACK、冲突不推进、重复入站不重复排队，并直接验证完整文本/图片/语音及跨请求/摘要拒绝。既有 Native 真实子进程/本地合成 HTTP/SSE 用例验证 own-message、分块和回执路径；clean code-view 用例证明平铺 Native 与嵌套 host 的依赖交付。发布审计、diff 检查和实际提交见本阶段报告 `.superpowers/sdd/task-2-report.md`（忽略草稿，不提交）。

未完成：Task 3 OneBot 服务/注册表、Task 4 网络/健康、Task 5 监督/恢复，以及 Task 6 全量/系统隔离/最终集成。没有真实账号操作、模型调用、部署或服务/网络变更；tmpfs 测试不证明物理磁盘掉电持久性。本任务实现等待独立审查，不表示整体架构完成。


Task 2 自审补充：兼容 wrapper 初版只收 positional 参数，新增旧关键字调用回归用例，RED 为 1 test / 1 failure（`unexpected keyword argument config`）。恢复原有五个命名参数后，`tests.test_wechat_adapter tests.test_onebot_native_forward.MediaLockDeadlines -q` 运行 18 tests in 0.813s / OK（含已跑用例的复验）。该次只改 wrapper 签名，未改 Native 业务实现。最终审计 300 files、findings=[]，diff 检查通过。

Task 2 独立审查：spec compliant、quality approved，无 Critical/Important 发现。Minor：HD 网关夹具的两条 asyncio 慢任务诊断，最终验收核查；非断言失败。物理持久性、systemd 隔离和最终全量仍待 Task 6，NapCat 采用点见本阶段记录。


## Task 3 实施记录（2026-11-12，历史记录；现已独立复审通过）

- 实现：OneBotService 组合 MessageService、DeliveryService、ActionRegistry；正式 host 使用 create_service，旧连续构造器仅兼容装配。JSON/响应/事件/整链解码归 onebot，core 持有路由/发布/窗口/分段/持久发送；WeChatPolicy 注入原始身份、准入/唤醒、正文与严格回执证明。协议/存储/IPC 格式、原群 ID 分配顺序、source/trigger/echo 幂等及未知发送保护保持。新增模块逐项登记 PUBLIC_CODE/host 视图；无 Native 资料挂载或凭据访问。
- NapCat 固定本地参考：72912f293924f43463b5976b01fc094a8ae11481；已读 action/index.ts、OneBotAction.ts、event/OneBotEvent.ts、event/message/*、index.ts 的 handleMsg、api/msg.ts 的转换器。采用 action map、公共校验/响应、原生事件转换分工；不采用 QQ 身份/SDK、TypeBox 类型强制、额外动作/别名/流或多消费者。保留本项目的严格有界协议、持久身份与先入队后 ACK。
- 下列命令均带环境：`TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B`。
- RED：`-m unittest test_onebot_services -q` → 5 tests / 11 failures（旧继承、好友/拒群分支绕过外层校验、缺少注册表/平台无关服务）。后续 `test_compatibility_assembly_uses_composed_engine` + `test_formal_host_uses_service_assembly_and_inventory` → 2 failures；新群 ID 次序/PrivateBridge 配置群目录的两项回归 → 2 failures。旧 continuous 导入别名回归另观察 1 test / 1 failure；修复后均包含于 GREEN。
- 回归中间发现并修复：组件缓存旧 state/媒体开关导致 196 项运行中 1 failure + 3 errors；改为读取同一状态所有者后，4 个原失败用例连同 all-groups 共 9 tests 通过。未屏蔽失败或放宽媒体验证。
- GREEN：`-m unittest test_onebot_services test_architecture_foundation test_wechat_adapter test_onebot_bridge test_onebot_continuous test_onebot_routing test_onebot_mentions test_onebot_context test_onebot_all_groups test_onebot_sender_names test_onebot_links test_onebot_images test_onebot_image_output test_onebot_voice_output test_onebot_output_policy test_onebot_loop_guard test_onebot_action_transport test_onebot_continuous_wire test_onebot_ipc test_onebot_boot_restore test_recovery_dedup test_durable_quarantine test_project_layout -q` → `Ran 264 tests in 24.231s / OK`。包含本地合成真实 aiocqhttp/WS/SSE/IPC/分块媒体回环和角色代码导入；有 0.353 秒 asyncio 慢任务诊断，未全局抑制。
- 边界：schema/SDK/IPC/资源限制/SQLite synchronous 未改。基线全量 I/O 中断仍未取得通过结论；Task 6 再集中全量。Task 4 网络/配置/健康、Task 5 监督/恢复、Task 6 集成/隔离验收未实施。没有外部网络、服务操作、真实消息/模型调用或部署。

Task 3 独立审查：spec compliant、quality approved，无 Critical/Important 发现；正式服务、非微信策略、整链验证、未知发送及 ID 分配顺序均有对应证据。Minor：0.353 秒 asyncio 慢任务诊断，Task 6 统一核查。真实部署、物理持久性及 systemd 验收仍未完成。


## Task 4 实施记录（2026-11-12，历史记录；现已独立复审通过）

基于 `ae4ca1d76ecd080fa81ed4a2d466b64aedb35b88`，实现 `transports.reverse_ws` / 内部 Endpoint、协议侧 `onebot.frames`、host 装配和共享 `core.health`。正式 host 调用 create_host → create_service；runner 只持有协议服务/事件源，不导入 SQLite、SDK 或 OneBot codec。旧 transport/Bridge 路径兼容导出或委托，不增加消费者。入口支持显式 store/backend/endpoint/时钟注入，默认路径/公开配置保持。16MiB action、256KiB IPC、认证头、队列/写入/ping/open/close、15 秒心跳与 1/2/4/8/16/30 + 300 秒重连预算保持。

健康：controller 的同一严格记录进入状态文件 `health` 和 IPC，get_status/heartbeat 委托同一个 HealthState。75 秒原始 Native 证明期限，没有更短 IPC 期限，不因重新发布延长；unknown/failed/stale=false。online 为账号事实，good/ready 还要求 SSE、WS、恢复准入，host 本地 WS 断开立即关闭 good。原消息/发送 operational 门控保留。Probe 本身也在 75 秒后撤销旧成功。新增字段均为非秘密布尔/时间/枚举/有限错误码。

纠正计划前提：此前 verify_role 仅检查存在/类型，sync 摘要没有握手消费者，AWG-READY 无版本。现在 controller-host 协商 protocol=2 和完整 host 角色公开源码 SHA-256，双端检查后才发/收绑定摘要的 AWG-READY/2；旧代码/版本/ACK 在 ready 前拒绝。Native 通道未宣称被版本化，仅将 health helper 显式映射到 flat `/opt/health.py`，保证 forward_wire 的实际 importlib/-I 加载；其他新模块/测试全部登记清单与受限 runner。不放宽隔离、只读、所有权、预算或 SDK/凭据边界。

NapCat：固定本地提交 `72912f293924f43463b5976b01fc094a8ae11481`，已读 `packages/napcat-onebot/network/index.ts`（NetworkManager/reload）、`adapter.ts`、`websocket-client.ts`、`config/config.ts`、`action/system/GetStatus.ts`。借鉴连接生命周期、事件源、动作分发与真实账号状态分离；不采用 QQ、JSON5、额外传输/热重载及 optimistic `online ?? true` / `good=true`。配置/schema/SDK/SQLite synchronous、恢复快照安全点与未知发送证据不变。

下列 unittest 命令环境均为 `TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B`。

RED：`-m unittest test_transport_health -q` 初次运行 9 tests，2 failures + 7 errors（缺少 health/transports/admission；其中一项 fixture 使用 sync helper 的错误签名，改为显式角色复制后单独复跑，缺少 role_digest 的预期 error）。ProbeFreshnessTests 单项 RED：1 test/1 error，缺少注入 clock/过期语义。AssemblyTests RED：2 tests，缺少 create_host 与受限 runner 登记。明确 online 回归 RED：1 test/1 failure，Native 未检查而 service.online 仍为 True；改为读取共享健康后 GREEN。新增 copied host/pipe 和边界断言均在最终矩阵执行。

GREEN（合成，无真实凭据/消息/模型）：

```bash
-m unittest test_transport_health test_native_health_probe test_onebot_services test_onebot_bridge test_onebot_continuous test_onebot_routing test_onebot_mentions test_onebot_context test_onebot_all_groups test_onebot_sender_names test_onebot_links test_onebot_images test_onebot_image_output test_onebot_voice_output test_onebot_output_policy test_onebot_loop_guard test_onebot_action_transport test_onebot_forwarder_pipe test_onebot_continuous_wire test_onebot_ipc test_onebot_boot_restore test_project_layout test_architecture_foundation test_recovery_diagnostics test_onebot_native_forward test_recovery_receive_integration test_recovery_disposition_integration.PackagingTests -q
```

`Ran 329 tests in 34.046s / OK`。覆盖实际本地 aiocqhttp/WS/SSE/IPC/媒体回环、重连六次后冷却、Native 真实子进程/本地合成 API、共享记录/同钟一致和 75 秒边界、匿名管道健康更新、匹配/旧协议/旧 ACK/变更源码的实际 copied host `python -I` 握手，以及源树/复制控制器 direct importlib 和 flat Native 代码视图。输出有一次既有大 action 的 asyncio 慢任务诊断 0.355 秒，未屏蔽；Task 6 一并分析之前各阶段诊断。中间专题 GREEN 为 71 tests/8.261s、74 tests/24.227s、282 tests/16.792s，存在重叠，不累加成独立覆盖数。

发布审计与 git diff 检查结果见本任务忽略报告 `.superpowers/sdd/task-4-report.md`。此状态是已实现、等待独立审查；不表示最终架构、全量或真实 systemd 隔离/宿主重启已通过。基线全量 I/O 中断事实不变。未部署、未操作 Native 会话/服务/Docker/AstrBot/FRP/宿主网络。Task 5 监督/恢复模块化和 Task 6 最终全量/隔离/交接待完成；tmpfs 验证不证明物理磁盘掉电持久性。

Task 4 自审补充：发现 runner 仍保留原有 send action 名称过滤，补 1 test/1 failure 的 RED 后，将写入成功后的 reply 分类移入协议 `reply_sent`，网络只回调服务。受影响复验 `test_transport_health test_onebot_action_transport test_onebot_bridge test_onebot_continuous_wire test_onebot_services -q` → `Ran 67 tests in 14.355s / OK`，有 0.346 秒既有大 action 慢任务诊断。该复验与 329 项矩阵重叠，不累加。最终发布审计通过（320 files、findings=[]），git diff --check 通过。

Task 4 独立审查：spec compliant、quality approved，无 Critical/Important 发现；共享 75 秒健康、纯传输职责及摘要绑定握手均确认。Minor 为已知 HD 动作 asyncio 慢任务诊断，非本阶段回归，Task 6 记录原因。实际 systemd 隔离、部署和全量仍待验收。


## Task 5 实施记录（2026-11-12，历史记录；现已独立复审通过）

基于 `8fd9b2570209af86897d468ede7cad6f99492b1c` 接续同一 managed detached worktree 的未提交实现，没有重做 Tasks 1–4。新增 runtime 的 controller/processes/session/state/broker/output/reports/failures 和微信 recovery 后端；正式入口委托新控制器，live core 引用保留 wrapper 的 UNIT/STATUS/ENTRY/SETTINGS。worker 错误包装、固定诊断、公开角色清单和受限 runner 同步更新；Native flat `/opt/worker_failures.py` 对应共享分类 helper。配置、依赖/SDK、资源和媒体预算、schema、持久 ID/IPC 表示及公开动作不变。

恢复：同一代错误帧与退出只计一次，故障优先于同批数据/ready；未知/身份/证据/权限/配置原因保持阻塞，只有明确连接/进程/就绪原因可在原 10 秒第三次故障预算内恢复。旧 unit/cgroup 停止必须核实，待决输出处置和 publishing→uncertain 必须持久成功，再关闭旧 capture、重建两端。任何失败都不能继续 spawn。重建请求/图片 assembly/ACK、输入和语音 relay、回复权限及媒体 lease；语音 preflight 对未知审计继续阻塞。不重复昂贵的 Native/startup admission，不 fresh login、不回退到最新游标。保留协议 2、host 角色摘要绑定的 ACK 与共享 75 秒健康。

补充两项恢复真值修复：durable ready 日志仅在控制器生命周期中提交一次，每代仍重新握手并 activate；snapshot reconcile 失败不能被后续空 replay barrier 改为 ready，也不能继续第二次捕获或发布保护。快照仍在单线程无请求/无媒体传输的安全点同步运行。

接续自审发现遗留未完成 RED：Native 在 Pump.start 后立即发 native_armed，它只表示线程启动。现在只有实际 sse_connection 与 WS 已连接、锚点握手完成才发一次本代 activate；SSE 报告前后顺序都可处理，WS 中途断开等待连接恢复，不重新选择游标。实际状态/心跳仍额外要求新鲜 Native 证明。

NapCat：重新阅读固定本地提交 `72912f293924f43463b5976b01fc094a8ae11481` 的 `packages/napcat-shell/napcat.ts`，采用进程所有权、主动停止排除和 10 秒/第三次故障监督。微信保留 root 控制器、两个受限 worker 和已固定完整会话；不采用快速登录、无法终止旧进程后仍继续拉起、或宽泛进程树清理。发送证据与持久队列是额外必需的重启门控。

所有下列 unittest 命令使用环境前缀 `TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B`。

接续前报告声称曾有 310 tests/49.058s、26 tests/0.412s GREEN，但会话中断丢失了前者的精确命令；该信息仅作为历史 checkpoint，不作为本次新证据。此前重复 host_ready/缺少锚点、重复 durable ready 和 reconcile 冲突的 RED 也仅有交接摘要。本次重新运行并保存以下完整证据：

- `-m unittest test_runtime_lifecycle -q` → 27 tests/0.408s，1 failure（native_armed 提前宣称 SSE 在线）。
- `-m unittest test_runtime_lifecycle.RuntimeIntegration.test_actual_sse_report_activates_once_and_commits_ready test_runtime_lifecycle.RuntimeIntegration.test_sse_connect_while_websocket_down_waits_for_link -q` → 2 tests/0.178s，2 failures；实现实际连接驱动后 runtime 29 tests/0.411s GREEN。
- `-m unittest test_runtime_lifecycle.RuntimeIntegration.test_sse_connection_before_armed_and_ws_reconnect_preserves_handshake -q` → 1 test/0.174s，1 expected worker_readiness error；定位为 WS 暂断时错误拒绝已握手的 armed ACK，修订后进入下面的 102 项复验。

Fresh GREEN 精确命令：

```bash
TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B -m unittest test_runtime_lifecycle test_worker_supervisor test_recovery_coordinator test_recovery_checkpoint test_recovery_manifest test_recovery_migration test_recovery_lifecycle test_recovery_receive_integration test_recovery_disposition_integration test_recovery_diagnostics test_onebot_native_forward test_onebot_forwarder_pipe test_onebot_voice_relay test_onebot_voice_native test_onebot_media_egress test_transport_health test_native_health_probe test_project_layout test_architecture_foundation -q
TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B -m unittest test_runtime_lifecycle test_transport_health test_onebot_forwarder_pipe test_onebot_native_forward test_recovery_receive_integration -q
python3.12 -B tools/audit_publication.py .
git diff --check
```

第一组 `Ran 267 tests in 40.455s / OK`；握手时序修订后第二组 `Ran 102 tests in 18.365s / OK`（包含 30 个 runtime 用例，与第一组重叠，不累加）。覆盖真实 SQLite 的 publishing 退休、真实短命 child 的错误+退出、真实图片/语音组装与未知审计、停止/审计/队列/媒体许可失败不重启、同代计数/第三次预算/兄弟排除、真实 formal serve 的单次 startup admission，以及 Native/匿名管道/copy 后 -I 角色导入与摘要握手。发布审计通过，331 files、findings=[]；git diff --check 通过。

状态为已实现、待独立审查。原始全量 I/O 中断事实不变；本次未跑全量、真实 systemd 隔离或宿主重启，没有部署/实际会话/真实消息/模型/现有服务操作。Task 6 完成统一全量、隔离验收与交接；tmpfs 合成验证不等于物理掉电持久性。增量报告保存在忽略的 `.superpowers/sdd/task-5-report.md`。

### Task 5 独立复审修复（2026-11-13）

独立复审结论为 `Needs fixes`，唯一 Important 项：替换代 worker 的激活没有遵守保留下来的恢复审查状态。`requires_review` 当时只影响 health 与状态展示；transient 重启后 `activate_if_ready` 仍向 host 发 `activate`，从而重新打开真实 DurableInbox 投递（`proxy.feed` 置 activation、`DurableInbox.next_message` 读 `next_pending`，两者之间没有 health/review 屏障）。已激活代在安全点证明失败后同样继续投递。

修复（在 `27a0fa4` 之上，工作树内 4 个文件）：

- `wechat_bridge/runtime/session.py` 新增 `admit_activation(s)`（`not s.recovery_state.endswith('requires_review')`），`activate_if_ready` 在原有前置条件之后、发送 `activate` 之前要求它成立。冷启动 `pending` 仍放行，否则首次准入自锁。
- `wechat_bridge/runtime/controller.py` 新增 `review()` 与 `stop_for_review()`；`stop_for_review` 停精确 unit、`RecoveryBackend.retire()` 持久退休 publishing、`workers.close()`，再抛 `Failure(stage='requires_review')`。`poll()` 在收集/重启之前先判定，保证不会先 spawn 替代代。
- `scripts/run_private_forwarder.py` 在 `capture_if_safe()` 之后立即判定 `review()` 并调用 `stop_for_review()`（同一次迭代内关闭投递窗口），并把 `requires_review` 加入 except 的 `recovery_coordinator.block` 集合与 `permanent` 集合，使其写盘阻塞且以 0 退出、不计入重启预算。
- 未改 IPC 帧、SQLite schema、持久 ID、admission 顺序或公开 action。

新增 4 个回归（`tests/test_runtime_lifecycle.py`）：`test_activation_gate_refuses_retained_review`、`test_retained_review_stops_generation_before_queued_delivery`（真实排队的 durable 行保持 `queued` 且不投递）、`test_current_generation_review_stops_delivery_and_never_respawns`、`test_formal_serve_blocks_coordinator_and_exits_on_review`。四个用例在回退实现后全部 FAIL、恢复实现后全部 OK（RED/GREEN 双向验证）。

验证（同一环境前缀）：

- `-m unittest test_runtime_lifecycle` → `Ran 34 tests in 0.418s / OK`
- `-m unittest test_runtime_lifecycle test_wechat_adapter test_onebot_routing test_recovery_coordinator test_recovery_lifecycle test_recovery_disposition_integration` → `Ran 112 tests in 20.813s / OK`
- `-m unittest discover -s tests -t tests -p 'test_*.py'` → `Ran 1232 tests in 72.006s / OK`

全量在本 clean worktree 首次通过（主仓库 main 基线在同一 tmpfs 为 1150 tests，且带一项不可读私有 staging 目录引起的 PermissionError）。本轮仍未跑真实 systemd 隔离验收、未部署、未接触线上服务。

Task 5 修复静态复审通过：原 Important 激活准入缺口已关闭，未重复运行作者的 34/112/1232 项验证。Minor：结束状态 requires_review 布尔仅看 image_hold，与已持久阻塞的 code 不一致；Task 6 做局部修正和单项覆盖验证。用户要求减少冗余测试，后续沿用已有全量证据，聚焦架构交付。


## Task 6 集成与交接（2026-11-13，整体静态复审通过）

基于 `321331165458cd754edd01563406b50565852a02`，实现与交接提交 `eb0c666`，没有重做 Tasks 1–5。交付[当前架构/AI 接手](modular-architecture-handoff.md)，更新 DEVELOPMENT 正式模块表与调用链、设计当前结论、AGENTS 接手入口和目录/恢复专题。NapCat 仍为本地 `.reference.json` 固定的源码快照 `72912f293924f43463b5976b01fc094a8ae11481`，采用点与微信差异逐项写入交接；不需要远端重读或运行 NapCat。

静态核对：core 不导入 SDK/具体适配/协议网络/systemd；storage 保留既有名称清洗辅助，core.delivery 只引用 StateError。transport 只持有协议服务/事件源，不导入 SQLite/SDK/codec。正式 host 经 create_host/create_service 实际运行 OneBotService/core/registry 和新 WS runner；controller 实际运行 runtime 控制器和 WeChat recovery backend。host 配置只有 OneBot token，没有 Native token，既有 mount 屏蔽 Native 私有状态；Native 仍显式 flat 映射，host 嵌套角色清单/协议 2/SHA-256 ACK 保持。旧 DB/ID/echo/publishing 与干净 `-I` 视图验证沿用已记录的全量/阶段证据。

两项局部源码修正：

- `run_private_forwarder` 最终 `requires_review` 布尔同时反映 image_hold 和保留的 runtime 恢复审查，解决 code=requires_review 而布尔=false 的 Minor。直接增强原 formal serve 回归的最终状态断言。
- `accept_reboot_recovery.stage_tree` 改用 `runtime_manifest.public_sources()`，替代递归收集全部 `.py`。未登记暂存/私有/符号链接文件不会被复制；已登记文件/父目录 symlink 或缺失源码明确失败并删除不完整 staging。保留 DynamicUser 可读/可遍历权限和所有原隔离/资源预算。3 项测试加入既有 `test_project_layout`，已在 PUBLIC_CODE/受限 runner 内，无新增测试套件或重复注册。

命令与结果（测试/模拟均带 `TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1`；unittest 另带 `PYTHONPATH=tests`）：

```bash
python3.12 -B -m unittest test_project_layout.AcceptanceStaging test_runtime_lifecycle.RuntimeIntegration.test_formal_serve_blocks_coordinator_and_exits_on_review -q
python3.12 -B -m wechat_bridge.simulation
sudo -n python3 -I -B scripts/accept_reboot_recovery.py isolated
python3.12 -B tools/audit_publication.py .
git diff --check
```

- RED：4 tests / 0.186s，4 failures，分别为复制私有/未登记源码、接受 symlink、接受不完整源码和最终审查布尔错误。
- GREEN：4 tests / 0.183s / OK。仅这些直接受影响用例，没有重跑 runtime 矩阵或全量。
- 模拟：退出 0，`ok=true`，全部场景标志 true；纯本地合成，无真实账号/模型。
- isolated：退出 1，`sudo: a password is required`；脚本未开始、单元未启动。没有索取凭据、降低限制或增加替代矩阵。有权限的单独窗口可从源码根运行同一命令；本轮不运行 live baseline/check/部署/恢复脚本。
- 发布审计：`python3.12 -B tools/audit_publication.py .` 退出 0，332 files、findings=[]；`git diff --check` 退出 0。静态清单核对 42 个模块文件全部登记，258 个公开交付路径、25 个 Native/65 个 host 角色文件均可解析到真实源码。

复用的全量是 `0452ba7` 的 1232 tests / 72.006s / OK（精确命令见 Task 5），不把它写成 Task 6 新跑的全量。最大图片 action 的 asyncio 诊断已由前一轮 profile 归因：夹具实际是 8MiB/128 块，保留的有界 JSON 结构扫描约 0.271s，AST 与原基线一致；未压日志、放宽校验或改后台快照。另一 HD 下载夹具未独立 profile，不能把所有诊断均归因。

`git show 5ce68e5:scripts/project_paths.py` 核对到原基线已固定 `wx-v1.4.0-linux-x64-gnu`；本轮只纠正/限定当前概览里过时的 v1.2 表述，没有升级 SDK、依赖或改历史升级记录。实际二进制及部署版本未知。

剩余：root 最终整体静态复审及真实发现；有权限的 systemd isolated 验收、真实宿主重启、物理掉电持久性、长期在线与生产部署均未验证。本阶段不 push/merge，不接触 Docker/AstrBot/FRP/Native 会话或宿主网络。忽略目录报告仅作临时明细，所有可接手结论已写入本 tracked 文档。


## 最终交付结论（2026-11-13）

原始基线 `5ce68e5` 至 `f8dfa42` 的整体静态复审批准源码集成，无 Critical/Important。唯一 README 模块导航 Minor 已在 `c8ba744` 修正并经静态复核关闭，没有剩余发现。复审核对正式六层调用链、存储源码兼容、精确进程停止/证据退休、审查阻塞、角色清单/摘要握手和凭据隔离；未重复测试。开发、设计、AI 导航和交接文档已同步当前完成状态。

沿用 `0452ba7` 的全量 1232 tests /72.006s /OK；Task 6 只补实际改动的 4 项定向验证与一次模拟，均通过。文档导航修订未运行测试；发布审计和差异检查在交付前核对。最大图片的既有慢任务诊断原因与适用范围已记录，无屏蔽或性能范围扩张。

改动保留在本附加 managed worktree，未 push、merge 或部署，未操作线上服务/真实账号、消息或模型。真实 systemd isolated 因 sudo 需要密码在脚本启动前被阻止，仍需有权限的独立窗口执行交接文档中的命令；真实宿主重启、物理掉电持久性和长期在线稳定性未验证。下一位 AI 从 [当前架构与接手](modular-architecture-handoff.md) 开始，不重做已完成阶段或重复无关测试。


## 私信灰度准备（2026-11-13）

用户已批准部署及受控首轮：现有绑定联系人、群收发全部关闭、用户发1条短文本/最多1次简短模型回复、暂不测试媒体。增加privateOnly正式配置，独立于allGroupsEnabled的旧单群含义；保留正式selfProof/anchor/activate及身份/速率/分段选项，三层限制群路由。六项新边界及84项受影响用例通过；静态复核发现的缺省allGroups覆盖privateOnly缺陷先加断言复现再修复通过，无剩余Critical/Important。

本次只读线上预检与账本证明、部署工具和接力顺序见[灰度记录](private-canary-rollout.md)。原桥failed、Native在线；不把源码完成或预检当作部署ready。未重复全量/真实发送/模型调用。后续以该记录中的实际切换状态为准。


### 私信灰度受历史记录阻塞（2026-11-13后续）

正式源码已快进到c255eed并写privateOnly=true/关闭媒体，尚未达到ready。真正阻塞是一笔未被原3项例外覆盖的live图片，原审计submitted且无jobId；它在现有保存快照中为needs_review/uploading，无法证明未送达。后续继续时Native owner已停止，既有约326.7MB快照可读，身份种子和桥接收锚点核对通过、无待决桥队列、无旧私信积压。当前等待用户明确同意将该唯一作业结果未知隔离、不重发，并复用保存资料恢复Native。未追加处置或恢复会话，真实消息/模型调用为0。下一位AI先读[实况](private-canary-rollout.md)，不要重新做重构测试矩阵。

用户随后已明确批准精确live图片的未知隔离且不重发，并恢复保存会话。当前执行限定恢复准备、审查及真实启动；不重复全量，新增工具仅补合成快照/账本保护的有效覆盖。完成/阻塞状态继续追加到灰度记录。

真实灰度已完成新增图片未知隔离（4项账本）并恢复同一Native，初次握手后因关闭图片输入仍发自动缩略图预热而保护停桥。已在Native能力产生侧最小修复，3项真实本地子进程/SSE定向验证1.152s通过；未放宽IPC/网络边界，下一步只重启桥、不再恢复Native。最新上线和模型调用结果见灰度记录。

最新4898d97正式灰度已ready（Native在线认证、真实SSE/WS、身份核验通过），privateOnly=true、有效群模式及媒体=false，四项准入有效；用户消息/模型调用0，待用户单条私信验证。此前握手后停止是历史过程，当前状态以灰度末段为准。

最终接力：owner旧64MiB/5%配额造成自动快照冻结过长，af9b608仅改有界预算并在线应用，不重启原Native/桥；预算相关5项0.008s通过。窗口后新鲜ready、Native/SSE/WS/身份均通过，群媒体关闭，真实收发/模型0，等待用户唯一私信。详情及保留的临时运维策略见灰度末段。

## 2026-11-14 私信用户事件跟进

用户已发送原定唯一短文本；发现Native认证断开，经一次保存登录resume恢复，原Native保留。桥随后保护停机；源码确认resume与捕获共用command.lock的竞争路径与现场时序一致，但通用错误码不足以证明唯一根因。完整快照/四项admission/身份/真实锚点/桥队列核对后原样归档journal，实际serve使用临时warm-only入口，拒绝新Native恢复。曾重新ready并只接收/转发原消息1次、群0；9字节回复经本次Native接受/投影GET核验且用户确认看到，但随后onebot_action_requires_review停桥，动作新增1笔ret1408、未决总54。慢磁盘/同步捕获与43/45秒IPC期限冲突方向待本地复现，未实现生产修复；未重发、追加模型调用或重复全量。保留失败保护及临时策略，详见[灰度最终结果](private-canary-rollout.md)。


## 2026-11-14 回执与捕获隔离修复（源码状态）

用户已批准按[修复设计](superpowers/specs/2026-11-14-receipt-recovery-isolation-design.md)与[计划](superpowers/plans/2026-11-14-receipt-recovery-isolation.md)实施。以下实现与验证记录形成于部署前，最终上线结果见本节末尾；部署前现场桥为failed，原Native在线认证，inbox仍2861 forwarded、无待交付记录，动作未决仍54。GET扫描267条后续事件，真实锚点与绑定联系人通过、private_incoming=0、未移动游标。

- `text_send_recovery`现在识别accepted审计绑定的唯一NULL/1408短文本动作；停桥、原Native PID/ticks与GET投影证明、完整备份及事务CAS通过后才修正该行。启动不再因accepted审计跳过这笔未知结果，不重发或修改历史其他动作。
- proxy保留最多32笔/300秒文本回执绑定，DeliveryService只对完整单段证明调用存储CAS。错误ID/目标仍拒绝，多段局部成功不报整体成功。没有补发WS响应或模型重试。
- 普通1400/1403/1501拒绝作为动作失败；1408文本进入发送/模型交付暂停，主控制器继续处理IPC和诊断。root审计须匹配requestId、分段key、目标摘要及sendId才能解除对应暂停。绑定随失败WS写回保留；迟到的旧失败报告不会重新锁住已核对动作。未绑定未知及其他动作的暂停不被一起清除，完整性/存储故障仍保护停止。
- 正式捕获为host动作/发布暂停 → Native暂停 → host FIFO落盘确认 → 固定Native/bridge副本 → 有界root作业证明/发布。host有在途动作时延后捕获，不暂停Native，不延长43/45秒发送等待。普通固定副本完成即恢复收发；有rebase待核对时保留暂停直到完整证明。作业300秒、1024/768MiB、100%CPU、swap0/tasks16，主循环不做大归档IO。
- `CaptureJob`只拥有一个精确unit及捕获token/worker代际，取消先关闭控制管道撤销提交权，再证明作业/cgroup停止。最终指针替换前重核父进程/Native/profile、处置账本和live保护范围/原barrier；清单提交后才推进两代最小保护下界。新刷新及容量请求跨作业保留，容量仍需两个独立周期。
- owner mailbox锁忙/busy/待消费命令有明确busy分类，尚未写入新请求时可以延后；其他快照或身份错误继续保护。新模块和测试已登记显式PUBLIC_CODE与受限runner。

已验证：回执/动作/owner锁专题81项1.640s、capture/runtime收尾49项0.816s通过；真实Native子进程合成暂停/恢复及真实捕获子进程的固定边界、慢复制撤销指针提交均通过。恢复migration/lifecycle/coordinator/manifest等142项通过；该批另有一个测试模块名写错导致整条命令失败，正确wire专题补跑6项6.470s通过，不能把失败命令写成全绿。首轮真实systemd受限108项通过，零跳过，隔离/资源/cleanup及Native/现有业务基线全部通过；收尾102项2.650s通过，最终真实systemd受限119项通过，零跳过；隔离/资源/cleanup、原Native及现有业务基线均通过。未重跑1232项全量，未发送新消息或调用模型。

参考仍为本地NapCat 72912f2：采用OneBotAction的动作异常边界和network独立生命周期、worker的消息回传/任务所有权。保留微信的root隔离、单次提交、持久CAS、真实锚点和提交顺序。`Store.pairs()`减少同次重复两代读取；更完整的归档验证复用、owner独立定时快照协调、真实七天稳定和物理重启仍未完成，不宣称所有IO延迟均已消除。

独立静态复审已完成：发现并修正“本地超时门未立即关闭”和“指针fsync后缺少最终撤销检查”两项Important；审查者独立运行两条新回归2项0.953s通过，批准当前源码及精确单笔对账/暖启动工具，无剩余Critical/Important。没有把旧重构批准作为本次批准。


### 已执行上线与最终核对（北京时间2026-11-14 23:21）

修复源码提交`46bf472`经独立静态复审批准，无剩余Critical/Important；正式源码已从8d68140快进到该提交，262文件显式代码视图已同步。本轮没有重启Native、恢复登录、发送新消息或新增模型调用。

精确限定工具在bridge/recovery/maintenance/command锁下核对原owner、私信配置、当前9字节accepted审计及唯一动作，执行原Native GET/投影证明、完整备份和单行CAS。原1408修正为ret0；其他动作逐行、inbox、游标和已有身份映射核对保持。成功动作842→843、1408为28→27、NULL仍26，历史53笔未决完整保留；没有重发，也未把历史例外当成重试许可。

随后在原root私有warm-only实际serve入口下启动正式修复代码，保留原owner/Native PID与start_ticks绑定。线上观测到捕获进入committing时主循环状态年龄仍1–2秒、Native未冻结；后续recovery=ready、capture_phase=null、捕获作业0，至少一个完整线上捕获/发布/保留周期结束。运行238秒时Native/SSE/WS/身份/ready全部true，pending_send_reviews=0，私信/群received/forwarded/replies均0，群和媒体保持关闭。此为无新消息观察，不是重复真实私信验收。

最终只读检查：forwarder和原owner均active，Native在线认证、会话不变；队列仍2861 forwarded、无其他状态；新清单与admission通过、ledgerRevision=4/四项隔离不变，未知数53。运行期status没有结束时的cleanup_ok/native_session_unchanged字段；旧inspector将缺失字段显示false，不能据此推断Native变更，实际PID/ticks另有精确核对。

最终本地证据为102项2.650s及真实systemd受限119项通过、零跳过，隔离/限额/cleanup和Native/业务基线通过；未重跑1232项全量。发布审计与人工差异核对通过。独立审查发现的两项Important都有先失败后通过的回归，审查者又独立复跑2项0.953s并批准。

**保留的灰度策略：** Restart=no、失败通知暂停、媒体维护timer暂停和原warm-only ExecStart继续保留，以维持本次“复用原Native”的范围；未恢复普通入口的冷恢复能力或开放群/媒体。下一位AI不要再次对账这笔已成功动作、重复消费恢复票据或重跑prepare/restore。日常收发与后续测试授权应分清：本轮没有新的真实消息预算。更完整的验证复用、owner独立定时快照协调、长期稳定/物理重启仍是单独后续工作。

## 2026-11-15 私聊媒体已启用

用户授权图片收发、语音输出和链接卡片输入；四开关已true且原Native复用、privateOnly与群关闭保持。切换发现捕获子unit的systemd停止顺序问题，`37ff509`修正Before+BindsTo与capture专用signal-first/异步stop、原cgroup证明及重复close；89项专题和真实systemd三场景通过。完整准入/GET身份/锚点及备份后归档本次shutdown review，warm-only启动后新鲜ready/SSE/WS/Native均通过。未新增真实媒体测试/模型调用，旧53笔未决与四项隔离保留；临时Restart/通知/维护策略仍暂停。设计、实际启用状态与复审边界见[灰度最新记录](private-canary-rollout.md)。
