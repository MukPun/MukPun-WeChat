# MukPun-WeChat 模块化架构设计

日期：2026-11-12；当前结论更新于 2026-11-13。用户已批准分阶段替换、兼容现有功能；要求每阶段同步设计、开发文档和 AI 接力记录。

当前：六阶段源码重构与文档交付完成；正式路径已切换，最终整体静态复审通过，无剩余发现。实际模块表/调用链、固定 NapCat 采用点、权限与后续起点见[当前架构交接](../../modular-architecture-handoff.md)，精确提交/验证见[阶段进度](../../architecture-rewrite-progress.md)。以下任务决策按阶段保留历史；当时的“待后续实现/复审/全量”不代表当前状态。

## 目标与逻辑边界
- core：平台无关的消息、会话、投递与持久化接口；不识别微信 ID 格式，不依赖 agent-wechat、OneBot 网络或 systemd。
- adapters.wechat：agent-wechat HTTP/SSE、身份/群判断、媒体、回执证明，以及跨受限 IPC 的代理。
- onebot：OneBot v11 编解码、事件、动作注册与处理器；保持当前已支持的子集。
- transports：现有反向 WebSocket 的鉴权、生命周期、心跳、重连和有界收发；只访问协议服务接口。
- storage：既有 SQLite 身份、会话路由、队列、检查点和幂等记录；不改变表结构、持久格式或数字 ID。
- runtime：工作进程生命周期、握手、IPC 中介、媒体中继与恢复协调。Native 资料和发送证据的语义仍属于微信恢复后端。

逻辑分层不增加进程；保留 Native 会话、root 控制器及两个受限 worker。OneBot 侧只拿到平台代理，不能取得 Native 凭据。旧入口作为兼容装配/导出层，正式路径应实际使用新模块，不建立第二套真实消息消费者。

## 契约
消息契约保留来源 ID、会话种类/原始 ID、发送者、时间、正文/媒体及展示资料；游标和锚点是不透明恢复标识。SDK 字段和微信 ID 规则在适配边界转换。
发送保持完整内容、目标、幂等键及绑定回执；结果为确认接受、确定拒绝、未知，保留分段结果。平台证明由微信后端负责，持久投递顺序由消息服务负责。
能力与健康来自适配器；网络层只调用消息事件流、动作分发及协议生命周期接口。配置仍来自现有严格校验文件，由装配层注入内部配置视图。

## 不变量
Python 3.12，保持当前依赖、Native 固定版本/补丁和全部资源限制。保留全群路由、指定私聊、真实 @、文本/图片/语音和分享卡片现有行为及能力开关。
入站先持久入队/检查点再 ACK；publishing 中断转 uncertain，不重新投给模型。出站预留幂等后提交一次，不把创建作业或 WS 写入当成业务成功。
保留未知发送审计、快照清单、处置账本和锚点证明；不通过删除状态、跳到最新、自动重发或重新扫码恢复。保留旧 SQLite、状态文件和配置兼容；不迁移生产数据。
本轮不增加 QQ、WebUI、HTTP 通道、OneBot API、SDK 升级或无限续租；不操作生产服务、Docker、AstrBot、FRP、主机网络或真实发送。

## 恢复与健康
错误帧与进程退出归入同一分类/监督入口，同一代 worker 只计一次，主动停止不算崩溃。每 worker 10 秒窗口内第三次崩溃才整体退出。
可恢复的进程/传输故障在完成安全清理后重新握手；身份/证据不匹配、权限/配置不合法和不能确认的发送保留原保护。旧 worker 未确认停止、审计/队列清理失败不得继续拉起。
重建每代会话的图片、语音、请求和 ACK 状态；不要重新执行昂贵的 Native 准入。快照仍在原安全点同步捕获，不引入后台并发捕获。
统一健康：online 是新鲜探测证明的真实账号在线；good 还要求 SSE、WS 和恢复准入可用。过期阈值为两次探测间隔加一次探测预算。未知/过期不报告在线；状态文件、get_status、心跳一致。

## NapCat 对照
本地参考：/home/mukpun/game/.references/NapCatQQ，提交 72912f293924f43463b5976b01fc094a8ae11481，来源见该目录 .reference.json。
每任务必须阅读对应实现，在进度记录说明采用的设计和必要差异：
- 原生封装：packages/napcat-core/index.ts、apis、helper/event.ts。
- 动作：packages/napcat-onebot/action/index.ts、OneBotAction.ts。
- 网络：packages/napcat-onebot/network/index.ts、adapter.ts、websocket-client.ts。
- 监督：packages/napcat-shell/napcat.ts。
- 状态/配置：packages/napcat-onebot/action/system/GetStatus.ts、config、index.ts。
参考职责和实现方式，用 Python 适配已有数据/权限约束；不引入 NapCat 的 QQ 实现或额外运行依赖。

## 验证与交付
每阶段先写有意义的失败用例，再迁移实现并跑受影响用例；独立复审后记录提交、命令、输出和遗留项。涉及子包同步，必须检查角色文件清单、只读挂载、摘要及 worker 导入。
最终跑全量 unittest、OneBot 模拟、发布审计及 Linux 隔离恢复验收；不能运行的验收明确记录，不能等同生产部署/真实重启已通过。
基线：原始提交 5ce68e554f4de309d8474d9176ccfa6953fb6048。此前全量测试因宿主 jbd2_log_wait_commit 长等待被中断（退出 130），没有得到完整通过结果；不得将此前基线写成通过。


## Task 1 实施决策（2026-11-12，独立复审通过）

- 交付：静态 `PUBLIC_CODE` 与角色映射集中在 `scripts/runtime_manifest.py`；同步支持嵌套包，保留根测试别名，旧清单只用于受限公开路径清理，不触及依赖或数据。拒绝源/目标父目录符号链接与 staging/private 路径。Native 打包与检查共用角色列表，当前 flat `/opt` 不变；未来 adapters 包同时扩展角色映射和导入。不新增 mount 或凭据访问。
- 契约：core 收纳既有 `NativeMessage` / `Receipt` 定义，旧 protocol 原样导出同一类；保留原有字段与 IPC/asdict 表示。新增会话身份/种类、三态发送结果、能力/健康数据及 backend/policy Protocol，避免引入调度/注册框架。backend 保持现有异步网关方法签名；实际适配、健康刷新和策略装配仍属于后续任务。
- 存储：`State` / `Spool` 实现移到 storage，旧入口只导出同一类和异常。除移动引起的 sender_names 相对导入深度调整外，实现、SQL、构造行为和持久格式不变。依赖测试禁止 core/storage 导入具体微信适配、协议网络或运行管理；旧层暂时保持现状，后续逐任务迁移。
- NapCat 对照：已读固定提交的 `packages/napcat-core/index.ts` 与 `packages/napcat-onebot/network/adapter.ts`。采用 core 聚合共享能力、协议/网络面向明确 backend 边界、网络生命周期与事件入口分离的职责思路。差异：本项目不把持有 SDK/session/logger 的 NapCatCore 传给网络，也不照搬其同时暴露 core/obContext/actions 的适配器基类；以不含凭据的 Python Protocol 和现有 IPC 代理保持进程权限边界。网络 open/close/reload 拆分留 Task 4，本阶段不新增传输或热重载。
- 验证：新增布局/架构、旧 DB 精确 ID/echo、publishing 退休、干净代码视图导入；专题结果见阶段记录。`.superpowers` 只留忽略的交接草稿，发布审计排除它。


## Task 2 实施决策（2026-11-12，历史决策；现已独立复审通过）

- WeChat adapter 是正式路径：host 直接装配 `WeChatIPCBackend`，旧 Gateway 名称保持类同一性；Native/SSE 脚本装配/导出包内实现。保留 Native flat `/opt`，通过 manifest 的 `wechat_native.py` / `wechat_sse.py` 显式源映射交付，不增加挂载或向 host 暴露 token。HTTP/媒体低层实现仍委托既有受限客户端；没有复制第二套业务实现或消费者。
- 原 Gateway 的 durable admission/checkpoint/队列消费提取到 `core.delivery`，平台策略注入 checkpoint 序列化与消息投影。core 不解析游标、锚点或微信 ID；`WeChatPolicy` 保持原格式和来源/路由检查。旧 continuous 的展示/图像消息类与投影移到 adapter.messages 并兼容导出，不改变数据字段。新的 receipts 三态投影是对既有证明的表示，正式 IPC 仍使用既有绑定回执与 flags，不能仅凭本地作业判 accepted。
- Native 主循环、恢复握手和媒体编排只是源码迁移，监督/恢复拆分留 Task 5；不顺便调整健康和恢复策略。保持 SDK 版本、发送一次、只重试确认读、媒体阶段判定、完整内容、echo、DB/IPC 表示和所有资源限额。
- NapCat 对照：固定本地提交下读取 `packages/napcat-core/index.ts`、`apis/msg.ts`、`helper/event.ts`。借鉴 Core 聚合明确 API、API 委托底层 session、事件 wrapper 用 checker 绑定异步完成的职责。区别：NapCat 注册 listener 后触发 sendMsg 并等待回调；这里维持 HTTP POST 一次、按既有 send/job ID 查询和消息投影证明，以及 SSE 事件先持久提交后 ACK 的恢复语义。超时不等于未提交，不将 callback 完成当微信持久证明，不把 session/core 凭据传给 host，也不引入 QQ 实现。


## Task 3 实施决策（2026-11-12，历史决策；现已独立复审通过）

- 正式装配：host 调用 `adapters.wechat.assembly.create_service`，构造 `OneBotService`，实际组合 MessageService / DeliveryService / ActionRegistry；不是对旧协议继承引擎套 facade。PrivateBridge/TextBridge 只兼容装配新服务；单次触发 Bridge 的旧语义留在 WeChat legacy 兼容入口，不在正式 host 中使用。transport 直接从 `onebot.codec` 获取动作解码，网络循环拆分与健康语义继续留 Task 4。
- 公共服务：MessageService 不检查平台 ID 格式；策略提供 conversation、来源/媒体准入、wakeup/body、accepted。OneBot 内容 codec 在任何预留/I/O 前完成整链验证，给 DeliveryService 传入 PreparedSend；core 不解析 OneBot 消息段，也不导入具体平台。保留原持久 namespace、会话/触发/echo key、请求摘要、分段作业键、窗口、取消/部分发送 unknown 及先预留后发送。新群 ID 分配顺序保持群 → 消息 → 成员；不迁移存储或调整 ACK。
- 协议：单一 registry 校验请求外层、opaque echo、params 类型和 self_id 后分发独立发送/状态能力/目录处理器。保留旧动作子集及返回形状；此前 PrivateBridge 特例绕过外层校验的非法请求现在统一 1400。旧 Bridge 与连续服务共享 codec/response/event/registry，同时保留各自既有发送权限语义。兼容可变 state/clock/媒体开关，组件读取同一所有者，防止已关闭状态库或旧开关副本被使用。
- NapCat：已读固定本地提交的 `action/index.ts`（getAllHandlers/createActionMap）、`OneBotAction.ts`（check/websocketHandle/OB11Response）、`event/OneBotEvent.ts`、`event/message/*`、`index.ts` 的 handleMsg，以及 `api/msg.ts` 的 rawToOb11Converters。采用显式 action → handler 注册、统一校验/响应、原生消息与 OneBot 事件投影分离。差异：不采用 QQ 原始数字 ID、QQ session/core 凭据、TypeBox coercion、QQ 宽消息段、_async/_rate_limited 别名、多适配器格式分发或流式动作；保持 SQLite 身份、严格字段/白名单、原错误码/echo形状、先持久 ACK、单一消费者和未知结果不重发。
- 验证：新增服务测试先观察请求校验/注册表/平台无关路径失败；构造器/host 清单和新群 ID 顺序/目录兼容分别补 RED 再 GREEN。最终 264 项专题通过，包含现有真实库的纯本地回环；清单/干净角色视图和 publication audit 通过。全量、systemd 隔离与真实部署不在本阶段执行。


## Task 4 实施决策（2026-11-12，历史决策；现已独立复审通过）

- 网络实现位于 `transports.reverse_ws`，唯一消息源和协议服务从装配层注入；runner 不导入 OneBot codec、SQLite 或 SDK。`onebot.frames` 接管有界请求解码、可唯一保留的 echo 和确定解析错误响应；原事件持久发布/失败回调、action/IPC 预算及重连/冷却均保持。`transport.py` 是兼容导出/连接工厂入口，正式 host 使用新 runner。
- `adapters.wechat.assembly.create_host` 只把既有配置转换为内部 Config/选项与传输实例，接收 state/backend/Endpoint/HealthState/墙钟/单调时钟；host 管理实例资源、IPC 和注入工厂。无公开配置或端点策略变化，无额外传输、API 或重载。旧单次 Bridge 同样委托共享 frame handler，保持旧业务语义。
- 同一健康记录由 controller 生成、写入状态文件并传给 host；严格验证键集、布尔/时间类型、连接枚举、有限错误码字符串与恢复枚举。有效期保持设计的 75 秒（两次 30 秒探测间隔加 15 秒读取预算），既看原始 Native 探测时间也看发布观察时间，不设不同的更短 IPC 阈值，不因重新发布延长证明。未知/失败/超期/时钟倒退 false；online 为账号事实，good/ready 再看 SSE、WS 和恢复准入。host 可根据本地 WS 断开立即关闭 good；同记录/同钟/同 WS 条件的 controller/get_status/heartbeat 计算相同。消息/发送层原有 operational online 与 activation 不改成账号在线，从而不放宽发送资格。
- 修正规划中的兼容准入假设：此前清单仅做文件存在/类型核验，sync 摘要不在握手中使用，AWG-READY 无版本。新增最小 controller-host 协议 2、实际角色源码摘要和绑定摘要的 ACK。controller 期望摘要来自当前公开源码；host 验证自身实际视图，isolated 回报再经 controller 比对。混合/旧代码在 ready 前关闭，旧公共配置/OneBot 响应不变。不是整个 Native 通道的版本化改写；Native flat 只增加无凭据 health helper 的显式映射，使 wire 在真实 `-I`/importlib 路径可用。
- NapCat 固定本地参考已读 network manager/adapter/client/config reload/GetStatus；借鉴生命周期、事件源、协议分发和账号状态的责任，保留微信的受限进程、单消息消费者、严格 JSON、精确资源预算和保守未知语义。不采用其 optimistic online/good、JSON5、QQ 身份/SDK、通用网络管理或 reload API。
- 验证包括 Native 离线/探测失败/75 秒边界、SSE/WS/准入分离、断开重连、服务拥有解码、实际匿名管道健康路由、实际 copied `python -I` host 协商/拒绝旧 ACK/混合源码、controller 直接 importlib 及 Native flat 视图。完整命令与结果见阶段记录；最终全量、systemd 隔离与生产部署不在本任务范围。


## Task 5 实施决策（2026-11-12，历史决策；现已独立复审通过）

- 正式控制器委托 runtime：processes 唯一持有精确 unit/capture/每代崩溃预算，session 管理摘要/隔离/身份/锚点握手，broker/output/reports 处理有界 IPC 与媒体，state 明确生命周期数据。入口保留正式 CLI/unit、配置注入、Native 准入、预算与状态。传入 live core 引用，避免提前缓存 wrapper 的 UNIT/STATUS/ENTRY/SETTINGS。微信专有快照/审计/固定会话处置由 adapters.wechat.recovery 承担。
- 单一故障路径先收集两端错误/退出，再处理普通数据；每角色每代计一次，停止兄弟进程不计入其预算，停止后尾部保护性错误优先于先前传输错误。只有显式 transport/process/readiness 原因可恢复；未知错误不能因诊断脱敏成 worker_failed 就获得重试许可。错误码/日志仍固定有界，PermissionError 不等于网络故障。
- 安全重启顺序：确认旧精确 unit/cgroup 停止 → 未知发送处置持久落盘并核对门控 → publishing 退休 → 关闭媒体 lease → 关闭旧 capture → 通过语音审计 preflight 新建 relay/lease → 新代重新握手。任一步失败立即阻塞，不再 catch 后继续 spawn。请求/图片 assembly/ACK、输入和语音 relay、last_input 权限全部换代；不重复控制器昂贵准入，不恢复未知发送，不跳过锚点。
- 就绪分开表示“线程已启动”和“真实连接已建立”：native_armed 仅证明 start 已受理，SSE 必须等实际连接报告；WS 已连接且锚点握手完成才 activate。SSE 报告可先于或晚于 armed，WS 中途断开等待连接恢复。每代 activate 一次，持久 startup ready 日志在同一控制器中只提交一次，避免第二代再写 receive_ready→ready 时误报完整性错误。共享 75 秒账号健康真值与摘要绑定 ACK 不变。
- 原安全点同步捕获保持；reconcile 证据失败立即结束本轮，不再被空 replay barrier 覆盖为 ready，不继续第二次 capture/发布保护。没有 SQLite/schema/SDK/资源限制调整或后台并发。
- 恢复审查（`requires_review`/`capacity_requires_review`）是投递准入条件，不是仅用于心跳的展示状态。`session.admit_activation` 在保留了审查的代上拒绝发出 `activate`，因此换代或重新握手都不会重新打开真实 DurableInbox 投递；冷启动的 `pending` 仍允许激活，否则首次准入会自锁。`Controller.review()` 在每轮 `poll()` 前、正式 serve 在 `capture_if_safe()` 之后立即判定，命中即停止精确 unit、经 `RecoveryBackend.retire()` 持久退休 publishing 并关闭 capture，然后以 `Failure(stage='requires_review')` 退出；入口把它视为永久阻塞（写入 `recovery-startup.json`）而不计入重启预算。空出队列既不重放也不清除审查，不 spawn 替代代，不后台重试。
- NapCat 固定提交 shell/napcat.ts 已读：采用 master 对 worker 的所有权、主动退出排除和 10 秒/第三次崩溃保护；保留微信完整会话及 root/两 worker 权限划分，不采用快速登录、best-effort kill 后继续启动或按进程树宽泛清理。Native flat helper/公开 manifest/受限 runner 同步更新。验证包括真实 SQLite、真实短命 child、合成媒体/匿名管道和 copied -I 角色视图；最终全量与 systemd 隔离不在本阶段宣称通过。


## Task 6 集成决策（2026-11-13，整体静态复审通过）

交付重心是实际依赖/正式装配/权限与源码角色核对、可供另一 clone/AI 使用的 tracked 文档。core/transport 的逻辑边界不等于增加进程或跨进程暴露 Native 凭据；旧入口保留兼容，不并行运行第二套消费者。Native 平铺映射和 host 嵌套包均继续由显式清单交付，controller-host 协议 2 与角色摘要保持。

验收 staging 必须与公开交付共用 `public_sources` 的显式清单/路径校验，不能递归搜集所有 Python 文件，否则未登记 private/staging 与文件 symlink 可进入受限验收视图。拒绝缺失/链接并清理半成品；保留 DynamicUser 目录/文件权限和原 sandbox 预算。最终状态的 requires_review 与 runtime 保留的审查一致，不仅检查图片 hold；投递准入修复本身已经在 Task 5 完成。

按用户要求减少重复测试：复用 `0452ba7` 的 1232 项全量与已有干净视图/旧状态证据，本轮只运行 4 项直接回归、一次模拟、审计和差异检查。实际 isolated 命令因 sudo 需要密码未启动，明确保留环境限制，不通过修改 runner、权限或资源预算绕过。源码没有部署，真实 systemd/宿主重启/物理持久性/长期稳定性仍需另外窗口。

当前资产路径在原基线已是 wx-v1.4.0-linux-x64-gnu；本轮只修正文档概览，不作 SDK 升级或推断部署版本。HD 最大 action 的有界结构扫描保留，既有 asyncio 诊断不掩盖、不据此扩大性能重构范围。


## 后续私信灰度决策（2026-11-13）

用户批准重构版先仅指定私信上线。privateOnly（默认false）覆盖全群/单群；Native、协议策略和IPC各自约束目标，正式私信通过完整身份、锚点与激活门，不复用旧探针入口。旧群身份种子只读证明与当前投递许可分离。存储/ID/审计/恢复不改schema、不删队列、不重放旧模型请求。设计细节与实际状态见[私信设计](2026-11-13-private-canary-design.md)和[灰度记录](../../private-canary-rollout.md)。
