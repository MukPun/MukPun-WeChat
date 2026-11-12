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
