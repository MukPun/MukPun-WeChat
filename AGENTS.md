# AI 开发协作入口

本文件面向接手 MukPun-WeChat 的 AI。先理解现有链路和运行约束，再修改代码。详细技术说明见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)，对外功能说明见 [README.md](README.md)。`AGENT.MD` 是本文件的兼容入口。

## 接手时先做什么

1. 阅读本文件、开发文档及 README；运行 `git status --short --branch`，识别用户已有修改。
2. 根据本次任务读取对应源码、测试和专题文档，不要把早期 PoC 当作现行入口。
3. 区分本地源码、脱敏发布副本、Linux 已部署副本和线上状态。修改源码不等于完成部署；旧验收不等于当前服务健康。
4. 未获得新的线上操作任务时，先在本地完成实现与模拟验证。仅为了解项目，不需要连接服务器、恢复登录或发送测试消息。
5. 报告实际修改、验证范围和未完成事项；不要声称完整 OneBot、无限期运行或绝对无风控。

现行行为以源码为依据，开发文档是导航。`docs/poc-*`、阶段验收和路线文档保留历史；其中“单群”“仅文本”“图片输出未实现”等叙述可能已经过时。2026-10-28 整理本入口时，代码已支持全群路由和真实成员名称。

## 项目定位与当前范围

- Linux 微信接入端的源码资产路径固定为 `scripts/project_paths.py` 中的 `wx-v1.4.0-linux-x64-gnu`（原始基线已如此，本轮没有升级 SDK），经受限 IPC 接入 Python OneBot v11 桥，再通过本机反向 WebSocket 连接 AstrBot。历史部署与验收状态见 [Native 升级记录](docs/native-1.2.0-upgrade.md)，不能从源码版本推断线上已切换。
- 桥负责传输、稳定身份、路由、去重、防循环、受限图片交付和回执；模型、人格、上下文、主动回复及插件属于 AstrBot。
- 正式配置默认 `allGroupsEnabled=true`，缺少该字段的旧配置也默认开启。收到新群消息后建立稳定路由；不是启动时枚举全部微信服务器群目录。
- 全群普通文本逐条上报；开启图片输入时，支持的普通群图片也可直接上报。只保留真实原生 @，不把普通消息或 `/ai` 伪造成 @，不改写全群模式的命令正文。是否唤醒由 AstrBot 决定。
- 私聊仍只绑定指定联系人。不要将“全群”误读为“所有好友私聊”。旧单群模式仍保留前缀、原生 @ 及上下文开关。
- 已实现持续 SSE、持久队列、文本分段、普通图片输入、受限单张静态图片输出、成员昵称/群名片透传和会话保存恢复。图片由桥主动发起 `media-transfers` 下载原图，读取期限 75 秒、整体预算 80 秒；图片上传阶段的确定失败不再中断文本桥，只有提交阶段之后的结果未知才停桥待核对。
- 缓冲合并明确交给 AstrBot `chat_plus`，不在桥内重复开发。原生 `type=47` 表情输入为独立开关，当前用户授权仅绑定私聊，实际启用见本文末段；WebUI 和网页配置编辑尚未实现。

## 代码地图

正式开发入口是 Git 工作副本。实例配置、二进制及依赖放在被忽略的 `.runtime/`；根私有会话仍留在系统状态目录。`scripts/project_paths.py` 统一派生路径，`tools/sync_runtime.py` 更新受限工作进程代码视图；详见 `docs/checkout-runtime-layout.md`。不得将整个私有目录提交或挂载给 OneBot。

| 任务 | 首选源码 |
| --- | --- |
| 正式配置 / 启停入口 | `wechat_bridge/forward_config.py`、`scripts/run_forwarder.py` |
| 控制器 / 进程隔离 / 审计 | `scripts/run_private_forwarder.py` |
| Native 接收 / SSE / 身份过滤 | `scripts/forward_native_worker.py`、`scripts/forward_sse.py`、`wechat_bridge/loop_guard.py` |
| 常驻 OneBot 事件与发送策略 | `wechat_bridge/continuous.py`、`scripts/forward_host_worker.py` |
| 协议公共契约 / 反向 WS | `wechat_bridge/protocol.py`、`wechat_bridge/transport.py` |
| 队列 / 身份 / 动作幂等 | `wechat_bridge/durable.py`、`wechat_bridge/state.py` |
| 生产 IPC / 请求回执绑定 | `wechat_bridge/forward_wire.py`、`wechat_bridge/forward_gateway.py` |
| 群路由及旧唤醒规则 | `wechat_bridge/group_trigger.py` |
| 昵称与群名片 | `wechat_bridge/sender_names.py`、`wechat_bridge/state.py` |
| 图片输入 | `scripts/image_native.py`、`wechat_bridge/image_input.py` |
| 图片输出 | `wechat_bridge/image_output.py`、`scripts/image_send_native.py`、`scripts/image_output_budget.py` |
| 会话 / 快照恢复 | `scripts/session_runtime*.py`、`scripts/session_state_store.py` |
| 重启恢复启动协调 | `scripts/recovery_coordinator.py`、`scripts/recovery_diagnostics.py` |
| 恢复清单 / 处置账本 | `scripts/recovery_manifest.py`、`scripts/recovery_snapshot.py`、`scripts/recovery_checkpoint.py`、`scripts/send_dispositions.py`、`scripts/recovery_dispositions.py` |
| 恢复迁移与验收 | `scripts/migrate_recovery_state.py`、`scripts/accept_reboot_recovery.py`、`tools/run_isolated_lifecycles.py` |

`gateway.py`、`ipc.py`、`bridge_*` 和 PoC 入口保留早期路径。常驻转发使用 `continuous.py`、`forward_gateway.py`、`forward_wire.py`，修改共享模块时检查两条路径的调用关系。

## 不可破坏的行为

- 使用真实原始 ID 确认身份与投递目标；数字 OneBot ID 由持久 SQLite 映射。昵称和群名片只供展示，不能作为唯一身份、授权或去重依据。
- 自身消息用已核验的真实 senderId、消息方向及多层准入拦截。不得按正文相同就删掉用户消息；用户可以重复问同一句话。
- 入站先持久入队，再保存/确认 SSE 游标。恢复时核对游标锚点摘要，不悄悄跳到最新，也不盲目重放旧提问。
- 入站状态为 `queued → publishing → forwarded/uncertain`；进程中断后残留 `publishing` 转为 `uncertain`，不自动重新调用模型。图片还存在明确 `expired` 状态。
- 出站先校验完整消息链、目标和权限，再持久预留动作，提交一次。幂等范围包含目标会话、触发消息和 `echo`；同一 `echo` 不同正文不能误用缓存。
- 创建本地发送作业不等于微信接受。仅回执绑定、必要投影及媒体校验通过才报成功。
- 图片 `preparing/uploading/readback` 阶段的确定失败可明确拒绝；进入提交阶段后结果未知须保留审计并等待核对，不能为恢复服务而自动重发或删记录。
- 不支持的 action/消息段明确失败，不截去图片只发正文，不伪造成功。WS 网络写入成功不是 AstrBot 业务处理确认。
- 用户会话应复用完整保存资料。连接或桥重启失败不触发 fresh login；不能随意 initialize、反复扫码、logout 或删除 profile。

## 资源与运维边界

- 不能影响现有 Docker、AstrBot、FRP（用户依赖它做内网穿透）；不重启这些服务，不改其配置或端口映射，不修改宿主防火墙/路由，不加载内核模块。
- 仅操作明确属于本项目的 unit、PID 和命名空间；禁止按模糊进程名批量终止。桥重启与 Native 会话重启是不同操作。
- 保留现有 cgroup、tmpfs、消息/IPC/图片、队列、运行期限及上传预算限制；不要通过去掉保护解决容量或超时问题。
- 图片输入依赖账号设置 `auto_download_max_bytes=0`（Native `registry.sqlite` 的 `account_settings` 表，不在本仓库代码内）；缺失时会退回 Native 自动下载队列，每张图多等 20 秒以上。迁移账号、恢复旧快照或重建 profile 后必须重新核对这个值。
- 当前 Native 单次上限 7 天；完整七天稳定运行、无限续租、完整 SDK 媒体生命周期和通用安装器未完成。上传源支持受限复用和恢复前离线回收，详见 `docs/image-8m-storage-128m.md`；不能把它当作运行中数据库清理接口。
- `scripts/restore_*`、`reconcile_*`、`deploy_*`、历史内核实验等具有状态或部署前提，不是可直接批量运行的安装脚本。先核对实际内容和精确适用条件。
- 持续转发现有消息不代表允许额外主动发消息。真实验证尽量复用现有交付证据；额外测试须明确目标、内容/类型、数量及模型调用预算，已有一次性发送授权不可重复使用。
- 用户关注 token 消耗，不用真实模型回显长文本；改用本地合成测试。


## 重启恢复与迁移

启动恢复不是"启动时顺手修复"，而是一条有持久阶段机的显式路径，详见[重启恢复说明](docs/reboot-recovery.md)。

- 阶段为 `validate → restore → online → rebind → receive_ready → ready`，每步落盘，可中断重入；`ready` 只在真实 WebSocket 与 Native SSE 就绪后由桥写入，协调器不提前宣称就绪。
- 失败分两类：`awaiting_network`/`awaiting_user_action`/`onebot_not_connected` 按 1/2/4/8/16/30 秒退避、用尽后 300 秒冷却；其余一律 `requires_review` 并以退出码 0 结束，避免击穿 systemd 启动频率限制。
- 并发启动由 `recovery-startup.lock` 互斥，第二个协调器读到 `recovering` 即退出，不抢恢复权。
- 恢复清单是不可变十键文档，绑定快照、账号资料指纹、处置账本版本、Native 事件边界和桥接收边界；接收边界必须带真实锚点，拿不到就明确阻塞，绝不回退到最新事件。
- 处置账本只为例外记录结论，每条绑定 `recordSha256` 与 `evidenceSha256`，不保存正文。快照里每个待决记录都必须在账本中有对应条目，新出现的未知发送不会继承旧许可。
- 保护下界只能在清单指针提交**之后**推进，且取当前与上一两代清单的最小值，绝不提前。
- 运行期捕获只在桥的单线程循环安全点（无待处理请求、无语音中继、无图片传输）发起，且用一个不跑构造函数的只读连接读桥数据库，不会替旧库补表。

迁移是一次性 root 操作，顺序固定：

```bash
sudo python3 -I -B scripts/migrate_recovery_state.py check   # 只读，不建表不写文件
sudo python3 -I -B scripts/migrate_recovery_state.py apply   # 全量备份后建窗口、导入遗留、发布首份清单
```

`apply` 要求桥未运行且没有其他恢复进程持锁；重复运行复用同一份备份目录，不覆盖第一份。`recovery_window` 表只能由迁移显式建立，不能指望启动时补建。

升级已装机器时注意 `scripts/run_forwarder.py` 的 unit 模板新增了 `StandardError=journal`，并把控制器预算改成 1024MiB/768MiB、100% CPU、`LimitFSIZE=1101004800`。旧 unit 文件与模板字节不一致会得到 `unit_definition_changed`，需 `stop` → 删除旧 unit 文件 → `install` → `daemon-reload` 一次。`daemon-reload` 不能替代删除文件，模板比对读的是文件内容。

控制器预算不是可随意收紧的余量。一条恢复路径要物化并校验约 209MiB 的快照归档与其 173MiB 的 Control DB：8MiB 的文件大小上限会让 `selected_pair` 直接失败（被统一报成 `recovery_integrity`），过小的 CPU/内存配额会让一轮同步快照证明从数十秒膨胀到数分钟，把转发循环饿死。同理，`accept_reboot_recovery.py` 的 `FRESH_SECONDS` 必须覆盖一轮证明，否则健康的控制器会被判成状态陈旧。

隔离生命周期验收（真实子进程、真实 SQLite、本地合成 Native API，不发送任何消息、不调用模型）：

```bash
PYTHONPATH=tests .runtime/verification/onebot-env/bin/python -m unittest tests.test_recovery_lifecycle
sudo python3 -I -B scripts/accept_reboot_recovery.py isolated
```

合成生命周期不等于真实宿主重启已验证；物理磁盘故障与在线长期稳定性需要单独窗口。

## 保密与发布

- 任何 API Key、OneBot/Native token、SSH 私钥、密码、二维码、微信 profile、身份数据库、快照和真实消息日志都不得写入源码、文档、提交或上传。
- 不把敏感值放进进程参数、可见日志、异常或 shell 命令；只输出必要状态/计数，排查日志先脱敏。
- 配置样例不含密钥。真实 `forwarder.json` 与运行状态留在实例私有位置；文档使用示例主机、用户、群和联系人。
- `.gitignore` 不能代替待提交内容审查。发布前运行 `python tools/audit_publication.py .` 并检查差异；审计通过也不代表可以跳过人工检查。
- 不直接提交整个部署目录，不上传 `acceptance/*-release/`、`.publish/`、运行库、虚拟环境和 SDK 二进制。

## 开发与验证

Python 3.12；运行依赖见 `requirements.txt`，测试依赖见 `requirements-test.txt`。无需微信账号或模型密钥即可验证协议层。

```bash
python -m unittest discover -s tests -p "test_*.py" -q
python -m wechat_bridge.simulation
python tools/audit_publication.py .
```

恢复相关改动额外跑 `tests.test_recovery_migration`、`tests.test_recovery_lifecycle` 与协调器/清单专题；涉及启动路径时用 `scripts/accept_reboot_recovery.py isolated` 在受限单元里复核。合成生命周期不等于真实宿主重启已验证。

根据改动运行相关测试，不为纯文档变更调用模型或重启服务。Windows 的 POSIX/systemd 用例会跳过；涉及 Linux 权限、管道、快照或隔离时，必须补对应 Linux 受限验证，不能用 Windows 通过替代。更多测试选择见开发文档。

跨层协议或文件清单变更要同步检查 Native、控制器、OneBot 及部署打包契约。仅在精确核对无不确定发送、完成所需检查后部署本项目；保留 Native 会话及已有审计，不自动执行旧恢复脚本。

## 后续开发方向

核心文本、上下文、防循环、普通图片输入、静态图片输出已有实际验收；全群路由与成员名称已有后续实现记录。尚需按用户下一次任务选择：长期稳定性与期限管理、媒体缓存和预算生命周期、安装/路径配置通用化、更多协议能力或 WebUI 设计。不要自行恢复表情包输入或缓冲合并计划，也不要把所有未实现功能当成本次授权。

## 模块化重构接力入口（2026-11-13）

六阶段原始源码交付已完成；后续私信灰度和2026-11-14回执修复已部署，真实systemd受限119项通过。原六阶段测试/复审只作历史证据，最新现场见本文末节。先读[当前架构与 AI 接手](docs/modular-architecture-handoff.md)、[阶段进度](docs/architecture-rewrite-progress.md)和[设计](docs/superpowers/specs/2026-11-12-modular-architecture-design.md)；只在需要核对原任务范围时读[实施计划](docs/superpowers/plans/2026-11-12-modular-architecture.md)，不要重做已完成任务。

当前源码入口是 core、adapters.wechat、onebot、transports、storage、runtime；上方旧代码地图中的 protocol/continuous/forward_gateway/state/durable/transport 已主要成为兼容入口。正式 host 经 `assembly.create_host` 组合 OneBotService/core 服务与反向 WS，正式 controller 委托 runtime，Native 保留 flat 角色映射。新增模块须更新 `runtime_manifest.PUBLIC_CODE`、角色依赖和相应受限 runner，不能递归收集源码或挂载私有目录。

全量 1232 tests / 72.006s 已在 `0452ba7` 通过；早期因宿主 I/O 中断的基线仍不算通过。用户要求减少冗余测试：沿用已记录证据，仅为实际源码改动跑有效覆盖；纯文档与静态复审不重跑全量。systemd isolated 尝试因 sudo 密码要求未执行，部署、真实宿主重启与物理掉电持久性均未验证。每次修改仍同步开发/设计/进度的实际状态；NapCat 固定本地快照与差异见当前交接。下一步是整体静态复审及真实发现，不能从当前源码推断线上状态或自动开启部署。


### 私信灰度接手（2026-11-13）

用户已给出后续部署授权：先仅绑定私聊、群收发关闭、用户发1条短文本/最多1次简短模型回复，暂不测媒体。先读[灰度实况](docs/private-canary-rollout.md)。privateOnly默认false，true覆盖allGroupsEnabled/旧单群；不要用allGroupsEnabled=false冒充关闭群。保留旧队列、身份映射、动作结果及Native例外账本，不把历史未决数当作可重发许可。通用root shell曾被自动审批拒绝，现使用审阅过的单次限定工具；不得记录凭据。后续群开放、额外真实发送或重复模型调用需新的明确范围。

2026-11-13后续实况：用户已批准唯一新live图片结果未知隔离且不重发，并恢复保存资料；四项账本完整。已恢复Native并在4898d97修复关闭图片输入仍发缩略图预热导致停桥的问题，随后正式privateOnly灰度达到真实ready，Native/SSE/WS与身份通过。尚无真实测试消息/模型调用，只等待用户一条短文本；不要再次运行prepare/restore、重做全量或打开群媒体。维护timer、首轮Restart=no和失败通知暂停仍保留，用户首轮验证后按灰度记录恢复精确项目策略。

最终预算接力：自动快照窗口还暴露root owner旧64MiB/48MiB、5%CPU限速；af9b608将其改为与恢复控制器相同的1024MiB/768MiB、100%有界预算，其他保护保留，现有精确unit已在线同步且未重启Native/桥。自然thaw后新鲜ready、Native/SSE/WS/身份通过，私信/群收发计数0，仍等待用户唯一短文本。不要再恢复或增加真实模型调用，先读灰度记录最后“最终接力状态”。

2026-11-14最终覆盖上述等待状态：用户唯一短文本已转发一次，9字节回复经本次Native接受/投影GET核验且用户确认看到；桥随后因onebot_action_requires_review保护停机，Native原会话仍在线。桥动作新增1笔ret1408（未决总54），与Native已接受证据不一致，禁止重发或删除。现场控制器长时磁盘写回阻塞与43/45秒IPC期限冲突方向待本地复现，未实现修复。精确临时ExecStart指向root私有warm-only入口，Restart/通知/维护仍暂停，勿盲目start、恢复策略或重复prepare/restore。接手必须读[灰度记录最终段落](docs/private-canary-rollout.md)，先处理已交付动作与回执/捕获协调，不再消耗真实模型验证预算。


### 2026-11-14 回执与捕获修复接力

用户已批准修复实施。先读[修复设计](docs/superpowers/specs/2026-11-14-receipt-recovery-isolation-design.md)与[灰度最新记录](docs/private-canary-rollout.md)，不要重做原六阶段重构。当前开发分支已实现accepted审计单笔对账、有界晚到文本回执/CAS、动作失败与发送暂停分离、host→Native→host落盘确认及独立捕获作业；后续正式源码已快进至46bf472并同步262文件视图；该笔动作已GET证明、备份及单行CAS为成功，历史53笔未决保留。私信桥已恢复，Native原PID/ticks、SSE/WS/身份/ready通过，无新消息或模型调用。

取消捕获先关闭控制管道再证明精确unit停止，不能遗留发布者后启动下一代。普通1408暂停发送和模型交付；重建host先恢复暂停再activate。完整性/身份/协议故障仍停桥。新增脚本/模块/测试已登记显式清单和runner。沿用1232项全量证据，只跑实际改动专题；最终systemd isolated119项通过，零跳过，cleanup与原Native/业务基线通过，详细证据见灰度记录。早期执行agent遇额度限制；后续独立审查已实际完成，修正两项Important后批准当前源码和限定对账/暖启动工具，无剩余Critical/Important。实际部署及现场证据见灰度末节，不单凭批准或本地测试推断线上健康。

自动审批拒绝过把捕获暂停窗口扩大到3600秒的方案；该方案未执行。实现保留100秒握手、300秒作业和43/45秒发送时限，host忙时延后捕获。群/媒体继续关闭，无新增真实消息/模型调用。不得盲目运行旧部署/prepare/restore工具或删除journal；本次临时warm-only入口、Restart/通知/维护策略仍需核对后处理。


2026-11-14 23:21北京时间最终状态：46bf472已部署，单笔1408→ret0且未重发，其他动作/inbox/游标/已有身份未改，旧53笔未决及四项账本保留；inbox2861 forwarded，无待交付。后台捕获完成后作业0、phase=null/recovery=ready，状态新鲜1–2秒，原Native在线认证、PID/ticks不变，私信/群本轮计数0。Restart=no、失败通知/维护timer暂停及warm-only入口保留，群媒体关闭，无新增真实测试预算。不要再对账此成功动作或运行prepare/restore。详见[灰度最终上线核对](docs/private-canary-rollout.md)。

### 2026-11-15 私聊媒体启用接力

用户后续已明确授权并开启图片收发、语音输出和链接卡片输入；四开关已true，privateOnly仍true、群和原生sticker关闭。`37ff509`已部署，修复捕获子unit停止顺序：Before+BindsTo配合capture专用signal-first与stop --no-block，仍先EOF撤销、证明终态/PID0/原cgroup空，成功close才幂等；其他unit默认同步stop不变。89项专题和真实systemd三场景通过，不能冒称重跑前轮119或1232全量。精确shutdown review经完整准入/GET身份/锚点/桥摘要及持久备份后归档，原Native未重启，桥已恢复新鲜ready；inbox2862 forwarded、旧53笔未决及四项隔离保留。未新增真实媒体交付测试或模型调用。原warm-only入口能力断言已更新，旧guard摘要/旧工具“媒体全关”假设不再适用；实际manager ExecStart已核对，勿重跑旧apply/prepare/restore。Restart/通知/维护仍按灰度暂停。先读[灰度最新段落](docs/private-canary-rollout.md)与[启用设计](docs/superpowers/specs/2026-11-15-private-media-enablement-design.md)。

### 2026-11-15 图片显示字段兼容接力

cb691c9已正式部署并同步262文件视图，兼容meme_manager的NapCat图片sub_type=0/1与有界summary，保留静态图片和原生sticker边界；62项相关测试通过，不重跑全量。两次切换停止分别产生native_changed和requires_review，具体触发原因未证；失败证据均保留，后者只在完整准入/GET身份/锚点/零积压、原Native/owner、所有旧worker退出及DB/审计/账本保持后，经受审两阶段工具精确归档journal并warm-only启动。不能宣称clean stop、已修复停止异常或再次盲目start。没有额外真实发送/模型调用，旧53笔未决与四项处置保留。新鲜健康、部署范围和后续限制以[灰度末段](docs/private-canary-rollout.md)与[设计](docs/superpowers/specs/2026-11-15-onebot-image-metadata-design.md)为准。

### 2026-11-16 现有链接已恢复转发

用户确认图片/语音正常；本次链接未到达发生于桥host exit1/worker_failed停机之后，实际Native已正常投影type49/app5/link及`[Link]`文本。无生产源码改动。完整准入、原Native/owner、旧worker退出、真实锚点/精确两条未消费输入及DB/审计/账本/journal保持后，一次受审warm-only启动恢复原游标，received/forwarded各2；捕获作业0后新鲜ready，原Native未变。已有输入正常触发回复，不能说模型调用为0，未额外主动测试或要求用户重发。host原始异常未留细分证据，尚未修复根因；Restart/通知/维护暂停及群关闭保持。禁止重跑本次单次恢复工具或旧prepare/restore；详见[灰度末段](docs/private-canary-rollout.md)和[链接专题](docs/link-input.md)。纯文档同步不重跑协议/全量测试。

### 2026-11-16 原生表情排查与已批准方案

用户重新提出表情输入问题，已完成只读排查：桥健康，stickerInput=false；实际type47/body=null已按旧策略跳过，媒体目录GET200/pending，未复现旧beta.8的目录500。当时尚未下载、启用或交付表情；不能拿普通图片成功代替。用户已明确批准具体实施范围，见[私聊表情方案](docs/superpowers/specs/2026-11-16-private-sticker-input-design.md)；旧“不要自行恢复表情计划”不阻止本次用户指定问题的只读排查，也不授权盲目启用/重播或重启Native。先沿用原Native与受限图片链，补有效worker覆盖、核验一张已有媒体，再按批准范围启用；不重跑全量、不增加占位模型调用。实况见[表情诊断末段](docs/sticker-input-diagnosis.md)与灰度末段。

本轮源码`50c3077`：仅表情读取75→60秒，普通图片预算不变；13项针对测试3.206s、独立任务复审通过，未重跑全量。批准范围允许一次已有表情下载及桥重启，不重复请求同范围授权。正式启用仍须原Native/owner、闲置捕获、精确停止、完整准入、受限下载及warm-only证明；以灰度末段记录实际结果，不能仅凭源码宣称已上线。

后续实际执行：50c3077已正式上线/同步262文件，唯一原样本一次下载尝试取得image/gif 14,327字节并通过完整性与策略恢复核验，未回放或额外调用模型。仅stickerInput置true，新warm-only guard SHA256 de71a4037cf5dcecddbd4f58f626da8c2846bf497ea853740f0344479aed94c3，五媒体true、privateOnly true、群/groupContext及原生表情输出关闭；Warm类/原owner/Native与限制保持，Restart/通知/维护策略不变。停桥出现native_changed但原Native/cleanup/宿主三基线均true，受审工具对精确completion/failure完整证明后私有备份，journal未改；不是clean stop，根因未证。旧guard摘要/四能力断言及单次probe工具不得重跑。真实AstrBot识图、完整动画和长期稳定性不由本轮取图证明；最终新鲜状态见灰度末段。

本轮最终只读证明通过：age1s、ready/Native/SSE/WS/身份true，capture0/recoveryready，原Native/owner、privateOnly/五媒体true、groupsfalse、inbox2879 forwarded/旧未决53、262文件视图匹配、CDN原策略恢复；新启动收/转/回均0。表情取图与桥开启已证，真实AstrBot识图/动画理解未额外验收。349文件发布审计0发现；整体复审与最终提交见灰度末段，不重做全量或本次一次性下载。

整体复审与最终健康补充复审均已批准。用户再次继续后的UTC 2026-11-16只读复核仍通过：当次控制器uptime27651秒、age0s、ready/Native/SSE/WS/身份true、capture0/recoveryready，原Native/owner和上述队列/代码视图不变；收/转/回仍0，未新增真实表情识图验收。后续从已启用状态接手，不再执行本次stop/adopt/probe/enable工具。
