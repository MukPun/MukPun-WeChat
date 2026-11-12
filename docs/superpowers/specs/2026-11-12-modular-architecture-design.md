# MukPun-WeChat 模块化架构设计

日期：2026-11-12。用户已批准分阶段替换、兼容现有功能；要求每阶段同步设计、开发文档和 AI 接力记录。

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


## Task 2 实施决策（2026-11-12，待独立复审）

- WeChat adapter 是正式路径：host 直接装配 `WeChatIPCBackend`，旧 Gateway 名称保持类同一性；Native/SSE 脚本装配/导出包内实现。保留 Native flat `/opt`，通过 manifest 的 `wechat_native.py` / `wechat_sse.py` 显式源映射交付，不增加挂载或向 host 暴露 token。HTTP/媒体低层实现仍委托既有受限客户端；没有复制第二套业务实现或消费者。
- 原 Gateway 的 durable admission/checkpoint/队列消费提取到 `core.delivery`，平台策略注入 checkpoint 序列化与消息投影。core 不解析游标、锚点或微信 ID；`WeChatPolicy` 保持原格式和来源/路由检查。旧 continuous 的展示/图像消息类与投影移到 adapter.messages 并兼容导出，不改变数据字段。新的 receipts 三态投影是对既有证明的表示，正式 IPC 仍使用既有绑定回执与 flags，不能仅凭本地作业判 accepted。
- Native 主循环、恢复握手和媒体编排只是源码迁移，监督/恢复拆分留 Task 5；不顺便调整健康和恢复策略。保持 SDK 版本、发送一次、只重试确认读、媒体阶段判定、完整内容、echo、DB/IPC 表示和所有资源限额。
- NapCat 对照：固定本地提交下读取 `packages/napcat-core/index.ts`、`apis/msg.ts`、`helper/event.ts`。借鉴 Core 聚合明确 API、API 委托底层 session、事件 wrapper 用 checker 绑定异步完成的职责。区别：NapCat 注册 listener 后触发 sendMsg 并等待回调；这里维持 HTTP POST 一次、按既有 send/job ID 查询和消息投影证明，以及 SSE 事件先持久提交后 ACK 的恢复语义。超时不等于未提交，不将 callback 完成当微信持久证明，不把 session/core 凭据传给 host，也不引入 QQ 实现。


## Task 3 实施决策（2026-11-12，待独立复审）

- 正式装配：host 调用 `adapters.wechat.assembly.create_service`，构造 `OneBotService`，实际组合 MessageService / DeliveryService / ActionRegistry；不是对旧协议继承引擎套 facade。PrivateBridge/TextBridge 只兼容装配新服务；单次触发 Bridge 的旧语义留在 WeChat legacy 兼容入口，不在正式 host 中使用。transport 直接从 `onebot.codec` 获取动作解码，网络循环拆分与健康语义继续留 Task 4。
- 公共服务：MessageService 不检查平台 ID 格式；策略提供 conversation、来源/媒体准入、wakeup/body、accepted。OneBot 内容 codec 在任何预留/I/O 前完成整链验证，给 DeliveryService 传入 PreparedSend；core 不解析 OneBot 消息段，也不导入具体平台。保留原持久 namespace、会话/触发/echo key、请求摘要、分段作业键、窗口、取消/部分发送 unknown 及先预留后发送。新群 ID 分配顺序保持群 → 消息 → 成员；不迁移存储或调整 ACK。
- 协议：单一 registry 校验请求外层、opaque echo、params 类型和 self_id 后分发独立发送/状态能力/目录处理器。保留旧动作子集及返回形状；此前 PrivateBridge 特例绕过外层校验的非法请求现在统一 1400。旧 Bridge 与连续服务共享 codec/response/event/registry，同时保留各自既有发送权限语义。兼容可变 state/clock/媒体开关，组件读取同一所有者，防止已关闭状态库或旧开关副本被使用。
- NapCat：已读固定本地提交的 `action/index.ts`（getAllHandlers/createActionMap）、`OneBotAction.ts`（check/websocketHandle/OB11Response）、`event/OneBotEvent.ts`、`event/message/*`、`index.ts` 的 handleMsg，以及 `api/msg.ts` 的 rawToOb11Converters。采用显式 action → handler 注册、统一校验/响应、原生消息与 OneBot 事件投影分离。差异：不采用 QQ 原始数字 ID、QQ session/core 凭据、TypeBox coercion、QQ 宽消息段、_async/_rate_limited 别名、多适配器格式分发或流式动作；保持 SQLite 身份、严格字段/白名单、原错误码/echo形状、先持久 ACK、单一消费者和未知结果不重发。
- 验证：新增服务测试先观察请求校验/注册表/平台无关路径失败；构造器/host 清单和新群 ID 顺序/目录兼容分别补 RED 再 GREEN。最终 264 项专题通过，包含现有真实库的纯本地回环；清单/干净角色视图和 publication audit 通过。全量、systemd 隔离与真实部署不在本阶段执行。
