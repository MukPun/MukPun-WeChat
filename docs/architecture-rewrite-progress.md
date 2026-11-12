# 架构重构接力记录

## 固定上下文
- 源码基线：5ce68e554f4de309d8474d9176ccfa6953fb6048。
- 工作区：本文件所在仓库；主仓库已有未跟踪暂存目录，不移动、不清理。
- 设计：[模块化设计](superpowers/specs/2026-11-12-modular-architecture-design.md)。
- 任务：[实施计划](superpowers/plans/2026-11-12-modular-architecture.md)。
- NapCat 本地参考：/home/mukpun/game/.references/NapCatQQ；提交 72912f293924f43463b5976b01fc094a8ae11481。
- 用户批准：分阶段替换，兼容现有能力；每阶段代码、开发/设计文档、交接记录同步。

## 验证基线
主仓库全量 unittest 曾因 jbd2_log_wait_commit 长等待而中断，退出 130；没有完整通过结论。已停止该测试进程；不修改生产服务。当前阶段优先运行必要的合成专题测试，最后集中全量验收。

## 任务进度
| Task | 内容 | 状态 | 提交/复审/验证 |
|---|---|---|---|
| 1 | 子包交付、公共契约、存储 | complete | `d460163`、`9f67d4b`；独立审查 compliant / approved，无发现；163 tests 通过 |
| 2 | 微信适配器 | complete | `965699b`；独立审查 compliant / approved；249 项专题及兼容检查通过 |
| 3 | OneBot 注册和消息服务 | complete | `47e4a9d`、`44cf087`；独立审查 compliant / approved；264 tests 通过 |
| 4 | 网络、配置、健康 | complete | `f1aeb71`；独立审查 compliant / approved；329 专题及 67 修订后验证通过 |
| 5 | 控制器与恢复 | implemented / awaiting review | runtime 与微信恢复后端；267 项专题及 102 项修订复验通过，待独立复审 |
| 6 | 集成、复审、交接 | pending | 依赖 5 |

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


## Task 2 实施记录（2026-11-12，待独立复审）

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


## Task 3 实施记录（2026-11-12，待独立复审）

- 实现：OneBotService 组合 MessageService、DeliveryService、ActionRegistry；正式 host 使用 create_service，旧连续构造器仅兼容装配。JSON/响应/事件/整链解码归 onebot，core 持有路由/发布/窗口/分段/持久发送；WeChatPolicy 注入原始身份、准入/唤醒、正文与严格回执证明。协议/存储/IPC 格式、原群 ID 分配顺序、source/trigger/echo 幂等及未知发送保护保持。新增模块逐项登记 PUBLIC_CODE/host 视图；无 Native 资料挂载或凭据访问。
- NapCat 固定本地参考：72912f293924f43463b5976b01fc094a8ae11481；已读 action/index.ts、OneBotAction.ts、event/OneBotEvent.ts、event/message/*、index.ts 的 handleMsg、api/msg.ts 的转换器。采用 action map、公共校验/响应、原生事件转换分工；不采用 QQ 身份/SDK、TypeBox 类型强制、额外动作/别名/流或多消费者。保留本项目的严格有界协议、持久身份与先入队后 ACK。
- 下列命令均带环境：`TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B`。
- RED：`-m unittest test_onebot_services -q` → 5 tests / 11 failures（旧继承、好友/拒群分支绕过外层校验、缺少注册表/平台无关服务）。后续 `test_compatibility_assembly_uses_composed_engine` + `test_formal_host_uses_service_assembly_and_inventory` → 2 failures；新群 ID 次序/PrivateBridge 配置群目录的两项回归 → 2 failures。旧 continuous 导入别名回归另观察 1 test / 1 failure；修复后均包含于 GREEN。
- 回归中间发现并修复：组件缓存旧 state/媒体开关导致 196 项运行中 1 failure + 3 errors；改为读取同一状态所有者后，4 个原失败用例连同 all-groups 共 9 tests 通过。未屏蔽失败或放宽媒体验证。
- GREEN：`-m unittest test_onebot_services test_architecture_foundation test_wechat_adapter test_onebot_bridge test_onebot_continuous test_onebot_routing test_onebot_mentions test_onebot_context test_onebot_all_groups test_onebot_sender_names test_onebot_links test_onebot_images test_onebot_image_output test_onebot_voice_output test_onebot_output_policy test_onebot_loop_guard test_onebot_action_transport test_onebot_continuous_wire test_onebot_ipc test_onebot_boot_restore test_recovery_dedup test_durable_quarantine test_project_layout -q` → `Ran 264 tests in 24.231s / OK`。包含本地合成真实 aiocqhttp/WS/SSE/IPC/分块媒体回环和角色代码导入；有 0.353 秒 asyncio 慢任务诊断，未全局抑制。
- 边界：schema/SDK/IPC/资源限制/SQLite synchronous 未改。基线全量 I/O 中断仍未取得通过结论；Task 6 再集中全量。Task 4 网络/配置/健康、Task 5 监督/恢复、Task 6 集成/隔离验收未实施。没有外部网络、服务操作、真实消息/模型调用或部署。

Task 3 独立审查：spec compliant、quality approved，无 Critical/Important 发现；正式服务、非微信策略、整链验证、未知发送及 ID 分配顺序均有对应证据。Minor：0.353 秒 asyncio 慢任务诊断，Task 6 统一核查。真实部署、物理持久性及 systemd 验收仍未完成。


## Task 4 实施记录（2026-11-12，待独立复审）

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


## Task 5 实施记录（2026-11-12，待独立复审）

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
