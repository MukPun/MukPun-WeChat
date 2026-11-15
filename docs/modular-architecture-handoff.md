# 模块化架构：当前实现与 AI 接手

更新：2026-11-14。六阶段重构已完成；后续回执与捕获隔离修复46bf472已部署到私信灰度，最新现场与保留策略见本文末节。原六阶段复审及验收记录是历史证据。[阶段进度](architecture-rewrite-progress.md) 保存提交与验证证据；[设计](superpowers/specs/2026-11-12-modular-architecture-design.md) 保存决策；[开发文档](DEVELOPMENT.md) 说明业务和运行约束。阶段记录中的“后续任务待实现”是当时状态，以当前进度表为准。

## 当前模块和实际调用链

| 位置 | 正式职责与依赖方向 |
| --- | --- |
| `core/contracts.py`、`messages.py`、`sending.py`、`delivery.py`、`health.py` | 消息/会话契约、路由与投递、持久入队、发送幂等及健康。策略和 backend 注入；不导入具体微信适配、OneBot 网络、SDK 或 systemd。`delivery` 仅引用 storage 的 `StateError`。 |
| `adapters/wechat/` | Native HTTP/SSE、消息投影、原始身份/群与唤醒策略、回执证明、IPC proxy、配置装配和微信恢复后端。SDK 字段、游标/锚点含义与微信 ID 规则在这里解释。 |
| `onebot/` | 严格 JSON/frame/content codec、OneBot 事件、统一请求外层校验、ActionRegistry 和现有动作处理器。组合 core 服务；保持已支持的协议子集。 |
| `transports/` | Endpoint 与反向 WS runner：鉴权、连接、重连、心跳、有界收发。只调用注入的协议服务/消息源；不导入 SQLite、Native、OneBot codec 或进程管理。 |
| `storage/` | 原 State/Spool 实现、SQLite 身份、动作、队列、检查点及路由。不依赖具体适配器、网络或 runtime；保留名称清洗辅助函数、表与同步写入方式。 |
| `runtime/` | 精确 worker unit 所有权、每代监督、握手、IPC broker、媒体输出/报告、故障分类及状态；调用微信 recovery backend 处理会话证据。不是平台无关的部署框架。 |

正式链路可按以下顺序定位：

1. `scripts/run_forwarder.py` 读取既有配置并绑定 `run_private_forwarder.py`。后者执行 Native/启动恢复准入、保留预算与状态汇总，实际监督委托 `runtime.Controller` / `Workers` / `State`。
2. Native worker 入口 `scripts/forward_native_worker.py` 装配 `adapters.wechat.native`；`scripts/forward_sse.py` 兼容导出 `adapters.wechat.sse`。隔离副本保留 flat `/opt/wechat_native.py`、`wechat_sse.py` 等显式映射，不挂载完整仓库。
3. host 入口 `scripts/forward_host_worker.py` 构造 Spool、HealthState、`WeChatIPCBackend`，调用 `assembly.create_host → create_service → OneBotService`。服务实际组合 `MessageService`、`DeliveryService`、`ActionRegistry`，网络实际运行 `transports.reverse_ws.Runner`。
4. 入站由 Native/SSE 经 controller broker 到 IPC proxy，`core.delivery.DurableInbox` 先持久入队和 checkpoint，再 ACK。MessageService 通过 WeChatPolicy 准入/建路由，OneBot EventCodec 投影，runner 发事件并回调持久发布结果。这里只有一个正式消息消费者。
5. 出站由 runner 交给 `onebot.frames`、registry 与 ContentCodec，先校验完整消息链，再由 core DeliveryService 预留幂等动作，通过 IPC/backend 发送。Native 受限客户端负责提交与绑定回执/投影证明；WS 写入或本地创建作业不算微信接受。

## 兼容、权限与恢复

`continuous.PrivateBridge/TextBridge` 只是组合服务的兼容构造器；`protocol.py` 兼容导出契约/codec/旧单轮 Bridge，`state.py` / `durable.py` / `forward_gateway.py` / `transport.py` 保留原入口。旧 `gateway.py`、`ipc.py`、`bridge_*` 的单轮路径仍在，不能当作正式常驻入口，也不新增第二个消费者。

常驻逻辑保持已有Native会话、root控制器及两个受限worker；捕获期间另有一个300秒上限的root作业，受控制器所有权和取消约束。`Workers.native_options()` 只向 Native 子进程的匿名管道交付 Native token；`spawn_host()` 只含 OneBot token 与非秘密路由/握手配置。host 只挂载公开代码/依赖与桥状态，Native 的 `/var/lib/astrbot-wechat-session`、`/run/astrbot-wechat-session` 被屏蔽，没有 profile/会话挂载。公开代码里存在 runtime/adapter 定义不代表 host 取得凭据或部署权限。资源、网络、只读挂载、所有权和媒体许可限制保持。

`PUBLIC_CODE` 是公开源码交付清单，`NATIVE_SOURCES` 是 flat 角色目标→源映射，host 使用嵌套包角色清单。`sync_runtime` 与隔离恢复验收 `stage_tree` 均调用同一 `public_sources`/路径校验，排除未登记暂存、私有目录、符号链接及不完整源码。生产 controller-host 在 ready 前核对 protocol=2、完整 host 角色源码 SHA-256 和摘要绑定 `AWG-READY/2`；混合/旧视图拒绝，不把整个 Native 通道宣称为已版本化。

恢复保持同代错误+退出只计一次、10 秒窗口第三次故障预算；只有明确进程/连接/就绪失败可重建。必须确认旧精确 unit/cgroup 停止，持久保存未知发送并退休 publishing，再关闭媒体和旧 capture，重新建立每代 relay/请求/ACK/握手。任一步失败不继续 spawn。真实 SSE 和 WS 连接、身份及锚点证明齐全才激活；`requires_review` / `capacity_requires_review` 同时阻止换代激活和已运行代投递，正式入口写盘阻塞并退出，最终状态也保留审查真值。空队列不能清除审查。

正式快照已改为host/Native暂停及FIFO落盘确认后，由有界root捕获作业固定副本、证明和发布；原同步包装仅留给旧trial/离线调用。未知发送/锚点冲突保留审计，不重发、不重投模型、不跳到最新、不 fresh login。SQLite schema、持久数字 ID、echo/错误形状、序列化 IPC、动作集合和媒体预算未变。健康证明保持 75 秒：online 表示真实账号证明，good/ready 另要求 SSE/WS/恢复准入，过期或未知为 false。

## NapCat 采用点与微信差异

参考是本地源码快照 `/home/mukpun/game/.references/NapCatQQ`，不是本仓库依赖；`.reference.json` 固定 `72912f293924f43463b5976b01fc094a8ae11481`（归档 SHA-256 `8bc74be5caba81edfb9f2fd2624597b14a6e681f9d005f21e1d57bde688b59f2`）。其他 clone 没有该目录时，使用同提交的 [NapCatQQ 源码](https://github.com/NapNeko/NapCatQQ/tree/72912f293924f43463b5976b01fc094a8ae11481) 核对职责，不将快照/二进制提交进本仓库。

| 固定源码 | 采用 | 保留的微信差异 |
| --- | --- | --- |
| `packages/napcat-core/index.ts`、`apis/msg.ts`、`helper/event.ts` | API 聚合、委托底层原生客户端、绑定异步完成 | 不把 session/core 凭据传给 host；HTTP 提交一次，SSE 先持久 ACK；超时不证明未发送，仍要求作业/回执投影。 |
| `packages/napcat-onebot/action/index.ts`、`OneBotAction.ts`、`event/message/*`、`index.ts` | 显式动作注册、统一校验/响应、事件转换分工 | 不引入 QQ 数字身份、TypeBox coercion、动作别名/流；保持 SQLite 映射、严格整链校验、echo 与未知发送语义。 |
| `packages/napcat-onebot/network/index.ts`、`adapter.ts`、`websocket-client.ts`、`config/config.ts`、`action/system/GetStatus.ts` | 网络生命周期、事件源、协议服务和账号状态分离 | 保留一个反向 WS、严格配置与资源预算；不增加 HTTP、多传输、热重载、JSON5 或乐观 online/good 默认。 |
| `packages/napcat-shell/napcat.ts` | worker 所有权、主动停止排除、10 秒/第三次崩溃监督 | 保留 root/两 worker 与完整 Native 会话，必须证明停止和持久退休；不快速登录、不 best-effort 停止后继续启动、不宽泛杀进程树。 |

## 验证证据与限制

实现提交：基础 `d460163` / `9f67d4b`，微信适配 `965699b`，OneBot `47e4a9d` / `44cf087`，transport/health `f1aeb71`，runtime `27a0fa4`，审查修复 `0452ba7`。Task 5 独立静态复审批准记录为 `3213311`；Task 6 集成/局部修正/交接提交为 `eb0c666`，最终整体静态复审已批准源码集成；唯一 README 导航 Minor 在 `c8ba744` 修正并复核关闭。

沿用 `0452ba7` 的全量证据：`TMPDIR=/dev/shm/wechat-modular-tests-spyge5c0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3.12 -B -m unittest discover -s tests -t tests -p 'test_*.py'`，1232 tests / 72.006s / OK。包括旧 DB/持久 ID/echo/publishing、真实本地 WS/SSE/匿名管道/媒体、干净 host 与 flat Native 的 `python -I` 导入/握手。Task 6 只调整验收打包、最终状态及文档，按用户要求不重复全量；这一数字是该提交的已有证据，不是最终文档提交的新全量结果。早期宿主 I/O 等待中断的基线仍不算通过。

Task 6 新增验证（同一环境前缀）：

```bash
python3.12 -B -m unittest test_project_layout.AcceptanceStaging test_runtime_lifecycle.RuntimeIntegration.test_formal_serve_blocks_coordinator_and_exits_on_review -q
python3.12 -B -m wechat_bridge.simulation
python3.12 -B tools/audit_publication.py .
git diff --check
```

四项定向用例先有 4 failures / 0.186s，再 4 tests / 0.183s / OK；3 项打包验证覆盖公开清单/嵌套目录/runner、文件和父目录 symlink 拒绝、缺失源码与失败清理；原正式 serve 回归新增最终审查布尔断言。模拟退出 0，`ok=true`，全部场景 true。发布审计与最终静态检查见[阶段记录](architecture-rewrite-progress.md) 的 Task 6 结果。

实际尝试 `sudo -n python3 -I -B scripts/accept_reboot_recovery.py isolated`：退出 1，`sudo: a password is required`，脚本未运行、单元未启动。未放松已有隔离/预算，也未运行 `baseline` / `check` / 迁移/恢复/部署脚本。真实 systemd 隔离验收需有权限的独立窗口；这里的合成结果不证明宿主重启、物理掉电持久性或长期在线稳定性。

已知 asyncio 慢任务诊断保留。最大图片 action 夹具虽名为 one_megabyte，实际构造 8MiB/128 块；先前 cProfile 显示保留的有界 JSON 结构扫描约 0.271s，AST 与原基线一致。未屏蔽诊断、放宽校验或改成后台快照；另一 HD 下载夹具未单独 profile，不能声称所有诊断均已归因。

源码 `scripts/project_paths.py` 的资产路径在原始 `5ce68e5` 已是 `wx-v1.4.0-linux-x64-gnu`。本轮没有升级 SDK 或依赖；历史 v1.2 升级文档仅代表当时记录，实际二进制和部署版本须在获得运维任务后单独核对。

## 下一位 AI 的具体起点

先运行 `git status --short --branch`，读 AGENTS、当前模块表和阶段进度。不要重新实施 Tasks 1–6，也不要因纯文档/静态复审重复全量测试。最终审查范围为原始基线到交付提交的正式入口、依赖方向、角色清单/凭据边界、旧状态兼容与恢复保护；修复真实发现后只跑对应有效覆盖。新源码必须登记 PUBLIC_CODE，Native flat helper 同时登记映射，新增专题须登记受限 runner。

源码与文档交付已完成。后续在取得 sudo 权限后另行运行保持既有预算的 isolated 合成验收；本轮脚本未启动，不能将该项标为已通过。生产部署、push/merge、真实宿主重启/扫码/消息/模型以及 Docker/AstrBot/FRP/网络操作均未授权。后续功能方向需用户另行指定，不自行增加表情包、缓冲合并、WebUI、SDK 升级或通用安装器。


## 私信灰度接力（2026-11-13）

后续用户已明确授权先上线私信模式。[灰度记录](private-canary-rollout.md)优先于本文此前“未获部署授权”的历史结论。privateOnly严格bool默认false；true覆盖群路由，正式worker仍持久游标/身份/activate，Native/host/core/controller均拒绝群收发。设计见[私信设计](superpowers/specs/2026-11-13-private-canary-design.md)、计划见[灰度计划](superpowers/plans/2026-11-13-private-canary.md)。没有新增SDK/OneBot动作/数据库迁移。

root只读恢复准入已通过，保留全部历史桥动作和Native处置；53条旧桥动作不允许重发。部署时只运行正式副本的限定工具，不用通用root shell，不读取/上传私有正文。首次启动的临时Restart/OnFailure覆盖仅属于本项目forwarder；验证后需恢复原策略。群开放仍等待用户后续授权。

当前灰度被一笔旧live图片及已停止的Native会话阻塞，正式副本c255eed尚未ready。现有快照身份/锚点通过、无私信积压；一笔新待决未被3项旧例外覆盖，等待用户是否将其未知隔离且不重发并恢复保存会话的明确回答。旧审批/真实发送授权不能覆盖它。详见灰度实况最新段落。

上述处置已获用户本次明确批准；正在执行唯一图片未知隔离与保存资料恢复。恢复不重做重构验收，先恢复Native/核对积压再起OneBot，维护timer首轮暂停；实时完成状态见灰度记录最后段落。

后续现场：隔离账本已精确覆盖4项，Native保存资料已恢复并在线；桥首次握手后因imageInput关闭仍发缩略图预热停下，Native保留。修复在Native能力门控，已作3项定向本地SSE/子进程验证；勿重新消费恢复票据。当前是否ready以灰度记录末段为准。

最新覆盖上述中间状态：4898d97已正式部署且ready，Native/SSE/WS/身份通过，群媒体关闭；四项隔离准入完整，真实私信/模型调用0。只等待用户首条短文本，不能再次prepare/restore，也不重复全量。首次临时Restart/通知/维护timer暂停尚需在该轮验收后恢复。

最终以af9b608 owner预算修复后的实况为准：精确owner已在线改为1GiB/768MiB/100%，其他限制保留，原Native和桥未重启；自然thaw后新鲜ready恢复、私信和群收发0。下一步只接用户那条短文本；当前维护timer/首轮控制器策略仍暂停，用户验证后恢复，不打开群媒体。

2026-11-14接力更新：用户原短文本已找到并只转发一次，群0；曾出现账号断开，保存登录resume成功后桥因保护停机，经完整只读门控原样归档journal并以原Native的warm-only实际serve重新就绪。宿主存在本次root私有guarded-serve入口和精确临时ExecStart覆盖，恢复正式策略前须看[灰度末节](private-canary-rollout.md)。接受回执须用本次新增动作核对，不能复用旧审计；不能再次让用户补发来掩盖该条结果，勿重新运行prepare/restore。

最终结论：本次9字节回复Native接受/投影GET核验通过且用户确认看到；桥随后onebot_action_requires_review保护停机、动作新增1笔ret1408，原Native仍在线。当前不是ready，也未修复慢捕获/IPC期限协调。临时guard/Restart/通知/维护仍暂停，所有结果保留；后续先本地复现并处理这一单笔已接受但桥未知的结果，禁止重发或追加模型测试。详情和计数以灰度最终段落为准。


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

## 2026-11-15 最新启用范围

用户已授权并开启绑定私聊的图片收发、语音输出和链接卡片输入，覆盖早期“媒体关闭”的现场记录；群和sticker仍关闭，warm-only及原Native身份限制保留。停止顺序修复`37ff509`已上线，89项专题和真实systemd三场景通过；精确shutdown review经完整证明、持久备份后归档，桥已恢复新鲜ready，历史53笔未决与四项处置不变，无新增主动消息/模型测试。root guard能力断言与实际加载ExecStart均已核对；旧工具的全媒体关闭假设及旧guard摘要不再适用，勿重跑旧apply/prepare/restore。真实媒体交付未重新验收；后续先读[灰度最终段落](private-canary-rollout.md)和[启用设计](superpowers/specs/2026-11-15-private-media-enablement-design.md)。

图片兼容更新cb691c9已正式部署：OneBot图片额外接受严格sub_type=0/1和≤256 UTF-8字节summary，修正meme_manager的NapCat显示提示拒绝，静态格式/来源/单次提交规则保持。62项相关测试及独立源码/工具复审通过，无真实发送或模型调用。切换期间native_changed和requires_review停止异常的原因尚未证实；已保存原失败，并仅在全部旧worker退出、原Native/完整准入/GET身份/真实锚点/零积压及DB/审计/账本保持后归档精确journal、warm-only启动。不要重复旧版重启或套用旧工具；新鲜就绪见[灰度末段](private-canary-rollout.md)，实现边界见[设计](superpowers/specs/2026-11-15-onebot-image-metadata-design.md)。

2026-11-16：用户已确认图片/语音正常。新卡片到达前host已exit1、桥worker_failed停止；原Native在线，卡片type49/app5/link与`[Link]`正文均有效，未改链接解析。一次受审暖启动在完整准入/原锚点/精确两条待消费输入/原Native及无旧worker证明后恢复，原两条received/forwarded各2，捕获结束后新鲜ready。保留失败证据、配置/账本及live journal，禁止重复旧恢复工具；正常回复来自原输入，不是额外主动测试。host原异常原因仍未证，不宣称稳定性修复。实际结果见[灰度末段](private-canary-rollout.md)。
