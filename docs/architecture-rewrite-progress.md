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
| 4 | 网络、配置、健康 | pending | 依赖 3 |
| 5 | 控制器与恢复 | pending | 依赖 4 |
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
