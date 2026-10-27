# Linux 云端 AstrBot 微信群接入方案

## 已确定的需求

- 接入普通微信群。
- 直接部署在 Linux 云服务器，复用已有 AstrBot。
- 模型服务及其配置由 AstrBot 负责。
- 先跑通一个测试群的文本问答，再扩展其他群和多媒体。

## 文章核对

用户已粘贴参考文章《2026年AstrBot接入微信群的实战指南》的内容。本方案参考“微信接入端 + 中转桥 + AstrBot”的分层思路，但不能直接照搬其中的工具组合：

1. **go-cqhttp 属于 QQ 协议端，不是微信登录服务。** 上游 README 明确说明其 QQ 协议来源，并已声明不再维护。它不能把微信账号直接登录起来。[上游说明](https://github.com/Mrs4s/go-cqhttp)
2. **OneBot 是协议，不是微信客户端。** 本项目用它连接微信桥和 AstrBot；收发微信消息仍由真实微信接入端完成。[OneBot v11](https://github.com/botuniverse/onebot-11)
3. **AstrBot 并非只能通过 Telegram Bot API 工作。** 它有多种平台适配器，其中 aiocqhttp 适配器支持 OneBot v11 反向 WebSocket。[适配器实现](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)
4. 微信图片、语音的表示取决于实际接入端；不能预设微信媒体一定由 go-cqhttp 返回 Base64。首版限定文本，后续按接入端实际能力扩展。

## 接入端选型

| 方向 | 本项目判断 | 原因 |
| --- | --- | --- |
| agent-wechat 原生 Linux 版 | 优先做技术验证 | 已发布 Linux x64/ARM64 beta 资产，提供原生 daemon 和版本化 HTTP/SSE 接口；仍有登录资料和 beta 限制 |
| agent-wechat 0.16.0 容器版 | 从零登录的备选 PoC | 容器内运行官方 Linux 微信与虚拟桌面，支持扫码；属于已被新版替换的旧 /api 架构，不能与原生 /v1 配置混用 |
| 腾讯官方个人微信 / AstrBot weixin_oc | 不能作为本次群聊实现 | 当前腾讯上游能力只声明 direct，不能用私聊成功替代普通微信群验收 |
| WeChatFerry / wxauto | 不作纯 Linux 首选 | 依赖桌面客户端及特定版本，不是本文所称“服务器部署 go-cqhttp 后登录微信”的路线 |
| itchat | 不作默认方案 | Python 可在 Linux 运行，但账号能否登录 Web 微信需要独立验证 |
| 旧 WeChatPadPro / Gewechat 教程 | 不作为已验证可用方案 | AstrBot 已撤回旧个人微信协议接入的官方支持，需要单独核实服务和账号可用性 |
| 企业微信智能机器人 | 目标变化时再考虑 | 企业内部群能力不能直接替代普通微信群 |

agent-wechat 原生候选固定为 [v1.0.0-beta.8](https://github.com/thisnick/agent-wechat/releases/tag/v1.0.0-beta.8) 做评估，不使用会漂移的 latest。其原生主线说明 Linux x64/ARM64、glibc 2.36+，主支持范围为接管完整的官方 Linux 微信 profile；运行时不需要官方微信窗口，但这不等于首次账号初始化不需要准备资料。[固定版本原生端说明](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/README.md)

部署前必须对固定版本核实上述要求、发布校验清单、账号初始化方式和群聊收发。若现有主机 glibc 太旧，优先评估隔离容器，不直接升级宿主系统。新 profile 登录属于实验范围，不能承诺空服务器一次扫码即稳定可用。若该候选无法完成测试群收发，先解决或替换接入端，不能把模拟消息联调当作微信接通。

若服务器没有可接管的资料，可先评估固定的 [0.16.0 容器流程](https://github.com/thisnick/agent-wechat/blob/v0.16.0/README.md)：已有 Linux 镜像和扫码登录流程，但需要 SYS_PTRACE、seccomp=unconfined 等运行条件，读取端含 Frida 依赖。先核实云服务器允许的隔离配置和镜像 digest，不直接运行 latest。它不能被描述为无注入或零风控的官方方案，也不意味着后续可直接迁移至原生版本。

当前旧社区 AstrBot agent-wechat 插件仍使用旧 /api 路径，而原生端已转向 /v1；本项目不直接复制该插件，桥实现前按固定版本实际 API 定义对齐。

## 项目职责

```text
Linux 微信接入端
    ↕ 本机或隔离网络中的认证 HTTP/SSE
本项目 bridge
    ↕ 认证的 OneBot v11 反向 WebSocket
现有 AstrBot 的 aiocqhttp 适配器
    ↕
现有模型、人格及插件配置
```

桥只负责消息转换、目标群限制、稳定身份映射、去重、收发状态和连接恢复，不持有模型 API 密钥，也不另外做模型调用。

## 首版行为和协议边界

- 只启用指定测试群；桥的群白名单为空时拒绝全部群消息。
- 默认只处理真正 @ 机器人的文本消息；接入端若不能可靠提供 @ 标记，先用严格 `/ai ` 前缀完成验证，不能用昵称子串假装可靠的 @ 识别。
- 群、用户使用接入端提供的稳定原始标识；如需转换为 OneBot 数字 ID，使用持久化 SQLite 映射，禁止 Python hash() 或群名作为唯一身份。
- OneBot 群事件保留 group_id、user_id、message_id 和文本/at 消息段；返回消息按原始会话标识发送，不按群显示名搜索并选择第一个结果。
- 自己发出的消息不再触发模型。使用接入端消息 ID 去重，重连不重放历史聊天。
- 同一会话顺序处理，配置限流和超时。发送成功必须来自接入端的实际结果；未知结果不盲目重发，不能一收到动作就先返回成功。
- 实现 AstrBot 文本问答需要的 get_login_info、get_status、get_version_info、send_group_msg、send_msg，以及 at 解析需要的成员查询；其他能力明确返回未支持。
- 日志记录连接状态、消息 ID 和错误；默认不保存聊天正文或凭据。
- 初期不主动插话，不做批量群发；图片、语音、视频放到后续里程碑。

## 对接已有 AstrBot

先读取实际版本与部署方式，再选择匹配的配置；不要求将现有实例升级到本次调研版本。

需要新增或复用一个 OneBot v11 / aiocqhttp 机器人：

| 项目 | 设置方式 |
| --- | --- |
| 机器人 ID | 独立命名，便于与已有机器人区分 |
| WebSocket 地址 | 按实际容器/宿主机网络确定，通常使用反向端点 /ws |
| 端口 | 如 6199；使用前先确认无冲突，不能覆盖现有监听 |
| 鉴权 | bridge 与 AstrBot 使用相同的 OneBot token |
| 模型与人格 | 使用用户在 AstrBot 内配置的选项 |
| 上下文 | 默认按群和成员隔离，联调时验证不串话 |
| 触发 | 与桥的 @ 或 /ai 策略一致 |

AstrBot 在 Docker 中时，`127.0.0.1` 指容器自身，不能直接当作另一个容器或宿主机。用户目前要求不影响现有 Docker，因而本次 PoC 不创建 Docker 网络、不连接或重建容器、不改变端口映射。后续 bridge 优先评估独立 systemd 服务与已有已发布端口；实际适配器配置和连接路径须在微信 PoC 成功后单独核对。桥与接入端不需要直接公开到互联网。

AstrBot 自带白名单的空列表语义与本项目桥不同，应按实际版本核实。桥本身始终执行目标群限制，避免依赖默认值。

## 实施顺序与验收

| 阶段 | 实施内容 | 完成条件 |
| --- | --- | --- |
| 0 环境核对 | 连接目标服务器；检查系统/架构、glibc、资源、现有 AstrBot 版本和网络 | 明确接入端可运行及 AstrBot 连接地址 |
| 1 微信 PoC | 固定接入端版本；完成测试账号登录；列出群；接收并向测试群发送标记文本 | 实际收到群消息，回复只出现在原测试群 |
| 2 桥实现 | Python 异步桥、OneBot v11、稳定 ID、白名单、去重、限流、错误回执 | 协议测试通过，失败不会误报成功 |
| 3 AstrBot 联调 | 连接已有 aiocqhttp 适配器，复用现有 AI 配置 | 测试群 @ / 前缀问题经 AstrBot 生成答案并回群 |
| 4 常驻运行 | 独立受限systemd服务；健康检查、会话保留与恢复说明 | 重启后优先无扫码恢复，白名单和身份不变，不重发旧消息 |
| 5 扩展 | 多群、图片与语音等 | 每项按实际微信端和 AstrBot 能力单独验收 |

文本闭环至少验证：目标群可回复、其他群不回复、未触发不回复、机器人不自循环、重名群不误投、断线可恢复、重启身份不漂移、发送失败可识别、不同成员上下文隔离。

## 当前交付和待补充信息

2026-10-25，固定微信原生端已通过离线启动、受限用户态联网、测试账号登录、指定联系人识别及私聊文本收发。随后完成完整会话保存与自动恢复，后续群验证不再新建空资料或重复扫码。

阶段1已完成：目标群“测试群”的随机标记接收、准确群名核对和获准单条发送均通过；微信服务器返回accepted后，用户明确确认“已在群内收到”。详见[群验证记录](poc-group.md)。这证明目标群文本收发链路可用，尚不代表AstrBot已接入。

阶段2及阶段3的单轮文本链路已通过：受限IPC将真实群消息交给宿主桥，连接真实AstrBot后处理一条`/ai 你好`并回传“你好！”，用户确认另一位群成员已实际收到。首个指定测试句未再次处理，因为单轮一次回复额度已被较早到达的这条新问答消耗。Windows477项测试通过（10项POSIX跳过），Linux桥/IPC43项全部通过，清理及Docker/FRP/网络基线通过。未由本项目修改AstrBot配置或重新扫码。

早期单轮联调之后，用户将转发层范围调整为参考Akasha-WeChat，重点完善持续接收与转发、防自回复；图片识别和所有模型调用交给AstrBot。2026-10-27用户进一步明确，缓冲合并由AstrBot的chat_plus负责，不在中转层开发；中转层保持逐条上报及有界持久队列。目前SSE、持续服务、普通图片输入和核心功能验收已完成，长期稳定性仍待验证。WebUI和在线配置编辑另行计划，以[转发层路线](forwarding-roadmap.md)为准。

已通过现有 SSH 配置连接服务器。用户硬约束为保护现有 Docker、frpc 并保持宿主内核不变；后续群验证继续复用既有保护，不加载新模块或改动宿主网络配置。不需要用户重新提供模型平台或模型密钥。

2026-10-25 新增优先约束：尽量避免重复扫码登录。会话保留、在线复用和真实账号自动恢复均已通过；群收发验证复用同一个在线连接，没有新的二维码、额外connect或重启。原临时测试的默认断开/销毁策略不再用于连续联调。详见[会话生命周期调整](session-lifecycle.md)。

账号接入使用第三方微信实现，能运行不等于官方承诺长期兼容。先用测试账号和测试群；限流用于控制机器人行为，不能保证账号不受限制。

## 核查来源

- [go-cqhttp 上游](https://github.com/Mrs4s/go-cqhttp)
- [AstrBot aiocqhttp 适配器 v4.29.1](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)
- [OneBot v11](https://github.com/botuniverse/onebot-11)
- [腾讯微信接口能力定义](https://github.com/Tencent/openclaw-weixin/blob/main/src/channel.ts)
- [旧个人微信方案官方支持变更](https://github.com/AstrBotDevs/AstrBot/issues/2353)
- [agent-wechat 原生端说明](https://github.com/thisnick/agent-wechat/blob/main/packages/native-client-rust/README.md)
- [agent-wechat v1.0.0-beta.8 发布](https://github.com/thisnick/agent-wechat/releases/tag/v1.0.0-beta.8)
- [agent-wechat 部署模板与迁移要求](https://github.com/thisnick/agent-wechat/blob/main/deploy/native/README.md)
- [agent-wechat 旧容器版本说明](https://github.com/thisnick/agent-wechat/blob/v0.16.0/README.md)
- [itchat 的 Web 微信登录实现](https://github.com/littlecodersh/ItChat/blob/master/itchat/components/login.py)
