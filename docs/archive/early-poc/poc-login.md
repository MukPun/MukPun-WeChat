# 测试账号登录实验

2026-10-24：测试账号已完成手机扫码确认，固定版本 Linux 原生端从空资料登录成功，返回 `succeeded` / `ok`。随后已确认断开并销毁临时资料。本阶段只验收登录，不代表群收发或 AstrBot 桥接已经完成。

使用 `scripts/run_login_probe.py --login` 与 `scripts/login_client_probe.py`；定位失败的可选模式为 `--login-diagnostic`，本次成功轮次使用该模式。控制器仅调用本次账号的创建、登录、operation 状态、二维码、取消与断开 API；没有调用群列表、聊天正文、同步或发送接口。上游登录成功时会启动后台 worker，可能存在短暂自动同步窗口；本控制器看到成功后先确认断开，再结束原生进程。

复用已有用户态 TAP、独立网络空间和共同资源上限：128 MiB、high 96 MiB、swap 0、CPU 20%、32 tasks。协议等待约 120 秒，外层最长 150 秒；资料与 token 仅存放在单元私有的 32 MiB 临时文件系统。本次结束后销毁，因此后续试验需要重新扫码。

只配置本次 guest 的地址和路由，helper 仅允许本次解析并校验过的微信公网 IPv4；DNS 关闭，客户端使用只读 hosts。没有加载内核模块、修改宿主路由或防火墙，也没有操作现有 Docker、FRP 或 AstrBot。IP 白名单不等于硬带宽或流量配额。

## 已修复的问题

1. 显式传入空 `BindPaths=` 会生成空 source，PID 1 报 `Source path  is not absolute.`，使登录单元在控制器启动前被拒绝。新 client 单元现完全省略 helper 专用的 BindPaths、DeviceAllow 和 IPAddressDeny，保留零 capabilities、PrivateDevices 与 closed device policy；helper 配置不变。
2. Windows ConPTY 会在第 80 列换行并重复字符，破坏单行二维码 JSON。root 控制器现在以每行不超过 68 字符的 AWP 帧传输。工具结果中的图片没有显示给用户，因此改用聊天正文 Markdown 图片及打开链接。PNG 仅短暂保存在本地任务的可视化目录，实验结束即删除；不写入源码项目、服务器持久文件或日志。
3. 手机确认后，微信实际要求连接 `szlong.weixin.qq.com`，但初始 files-only hosts 只含默认两域。仅观测测试进程的 DNS 诊断明确得到该域 EAI_NONAME (-2)，并且默认两域解析均成功，因而定位了认证失败的直接原因。
4. 手工固定加入 `szlong.weixin.qq.com` 和已独立解析验证的候选 `szshort.weixin.qq.com`；四域均重新校验公网 IPv4、排除宿主和 FRP 地址，并绑定只读 hosts。没有自动扩展任意重定向。成功轮次实际观察到 szlong、szshort 均被调用且返回 0，确认了本次区域配对。

诊断库为项目内 `login_dns_probe.c` 编译的 16528 字节共享库，SHA-256 `e6d908008c1284eb56d4b69a44d66a214881d1593c94685513c0f646a3420654`。只在可选诊断模式的原生子进程中启用，保持真实解析返回值和 errno；只向受限非阻塞 pipe 写入已过滤的微信服务主机名及结果整数。不改变 DNS、不接触聊天内容或凭据、不作用于宿主其他进程。使用现有 gcc、普通用户和进程资源限制构建，没有安装软件。

## 本轮实测结果

- 成功单元 `astrbot-wechat-login-84404e4a7621.service`，经过 starting → awaiting_scan → authenticating → succeeded；用户已在手机确认。
- `login_result` 为 `ok=true, state=succeeded, code=ok`，最外层 `login_probe_finished` 为 `ok=true, code=ok`，SSH 命令退出 0。原生端已确认断开并正常退出。
- helper 实际 IP 计数为出站 51506 字节、入站 58662 字节，仅作为观测值。
- 登录、helper、keeper 和父 slice 均已精确停止，TAP、staging 和临时资料清理完成。host_namespace_unchanged、host_network_unchanged、business_processes_unchanged 均为 true；IPv6 RA 正数计时器自然变化单独记录。
- 本项目 160 项离线测试通过。后续需要重新扫码，验证测试群识别、接收与受控回复，再与 AstrBot 联调。

早期二维码等待超时不能归因于用户未操作：当时图片仅在工具结果中，用户没有看到。显示修复后已实测手机确认；认证失败又经上述区域解析修复解决。

## 下一步：指定联系人私聊接收

用户指定先与“测试联系人”测试。新增显式模式 `--receive-contact`，默认登录模式仍在成功后立即断开，外层仍为 150 秒。接收模式在登录成功与断开之间使用剩余预算；2026-10-25 将其外层最长时间调整为 930 秒以容纳扫码和协调联系人发送测试文本，128 MiB/20% CPU/32 tasks 上限不变。收到口令立即结束。

先检查登录 operation 的 profile.state；仅 created/existing 进入联系人读取。pending/unavailable 报告固定原因并结束，避免把不可读数据库误报为找不到联系人。读取经过昵称筛选的联系人目录，必须完整取得有界分页且精确唯一命中 displayName，再绑定内部 username。目录不完整、重名或缺失时停止，不调用全量 contact-sync。

成功绑定后输出随机 `AWTEST-xxxxxxxx` 口令，请对方发送。最多等待 600 秒，仍截断于剩余工作期限；只从目录页游标之后的事件中筛选目标私聊、live、incoming，再读取对应单条文本以比较口令。只报告是否收到及固定的计数，不打印正文或真实联系人标识，不调用发送、read-state、ack、媒体或手动同步接口。上游登录自动启动的 worker 行为仍存在。

完成、超时或失败均回到已验证的断开与清理路径。私聊通过后，发送测试消息的具体内容仍须先与用户确认；本模式没有发送能力。临时会话不保留，下一轮需要重新扫码。

### 2026-10-24 私聊模式进展

- 接收模式实现完成，包含 profile gate、完整本地页的精确唯一联系人绑定、事件筛选及一次性口令比较。188 项离线测试通过，四份上传脚本已校验本地/远端 SHA-256 一致。
- 首轮 `99cde9d63a22` 在隔离检查阶段退出，没有启动原生端或发生网络流量；未能从该轮输出确定具体失败条件。新增 metadata_failed 安全阶段码，并把现有 32 地址上限前置检查，没有放宽隔离或增加地址预算。
- 重试 `astrbot-wechat-login-04ac7401da12.service` 使用 11 个已校验公网地址，隔离检查通过并显示二维码；最终 `deadline_exceeded`，未进入 authenticated/profile/联系人读取阶段。此结果仅说明本轮未在预算内完成登录，不能据此判断用户是否曾操作手机。
- 重试流量观测：出站 96804、入站 91760 字节。两轮均精确清理自己的 client/helper/keeper/slice，host_namespace_unchanged、host_network_unchanged、business_processes_unchanged 均为 true。没有修改现有 Docker、FRP、AstrBot 或宿主内核模块。
- 当前尚未验证联系人目录状态、“测试联系人”是否可唯一识别以及实际收消息。下一轮等用户准备好扫码再启动；发送功能仍未启用。

### 手机确认后的目录读取

`astrbot-wechat-login-69d31a9be436.service` 已实测 awaiting_approval → authenticating → profile.state=created → succeeded；本轮再次确认测试账号可登录且能自动创建资料库。联系人第一条 GET 返回 transport_failed，未进入 target_matched，也没有读取目标正文或发送消息。登录成功与接收失败分别报告，外层为 contact_receive_failed / exit 1。

该轮确认断开、停止原生端并销毁临时资源，宿主 namespace、网络和 Docker/FRP 进程基线均 unchanged=true。流量观测出站 66110、入站 86976 字节。

固定上游源码显示联系人读取和首次 push/sync 共用账号锁，上游网络调用有 30 秒超时；1 秒的探针读取策略可能过短，但本次错误分类不足以确定原因。现将目录/单条目标消息读取改为最多 min(30 秒, 剩余工作预算)，事件日志读取仍最多 1 秒，不重试、不提高整轮 150 秒或资源限额。通信错误仅输出固定 timeout/connection/protocol/encoding 分类，不输出原始异常。修正后 192 项离线测试通过。

profile created 只证明空表/keys 初始化成功，不证明微信通讯录已同步。后续目录为空时仍报告 target_not_found，不自动执行全量 contact-sync。

### 2026-10-25 扫码交互窗口修正

`astrbot-wechat-login-81d4a51e13a8.service` 在本地预算内未取得登录成功，返回 deadline_exceeded；用户随后反馈手机已确认。不能据此否定手机端操作。该轮未进入 profile/目录读取，已清理所有临时资源；宿主 namespace、网络及业务进程基线均 unchanged=true。流量为出站 96493、入站 91718 字节。

仅在显式 receive-contact 模式启用固定 extended_window：原生登录请求 timeoutSeconds=300，控制器总预算 330 秒、协议仍预留 2 秒清理，外层单元及网络持有者最长 360 秒。默认 login/qr-only 的请求和时间限制不变，不新增 API 路由、不自动刷新二维码、不重试目录查询、不扩大网络目标或 CPU/内存/tasks 上限。200 项离线测试通过。

固定版本接受 10–900 秒的原生登录超时，其默认值也是 300 秒；未扫描二维码的协议本地期限为创建后 240 秒，扫描后受操作总期限约束。因此本次主要修正原有探针过早退出的问题，不承诺微信服务端一定接受到本地期限。源码见 [runtime.rs](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/runtime.rs#L498) 与 [client.rs](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-protocol/src/client.rs#L361)。

另已将四个固定域名的候选地址各限制为最多 8 个，总数保持不超过现有 32 上限。先校验整个 DNS 返回值，再缩小候选集合；后部出现宿主或私网地址仍拒绝。

### 首次识别指定联系人通过

`astrbot-wechat-login-c743d42824d1.service` 已实测 authenticating → profile.state=created → target_matched，首次精确唯一识别“测试联系人”，证明修正后的目录读取和目标绑定可用。控制器给出口令后等待 60 秒，最终 message_not_received。用户反馈对方已发送；当反馈被处理时该轮已结束，不能判断实际发出时间，也不能据此确认是消息通道故障。

该轮确认断开并清理所有临时单元，宿主 namespace、网络、业务进程基线均 unchanged=true。出站 46276、入站 68691 字节。

为容纳两人之间的人工协调，仅将显式私聊模式接收窗口改为最多 600 秒，控制器总预算 900 秒、外层临时单元 930 秒；原生扫码操作仍为 300 秒，默认登录/qr-only 不变。CPU/内存/tasks/tmpfs/公网地址范围均保持原限制。新增一次最终 contact_observation，只含事件总数、目标 incoming/live 数、已校验文本数和是否匹配的布尔值，不包含正文、ID、游标或异常原文，用于区分没有事件、被规则过滤及口令未匹配。超时或收到口令均立即进入清理。修正后 208 项离线测试通过。

### 2026-10-25 私聊接收实测通过

成功单元 `astrbot-wechat-login-6e0356133b6c.service`：从空资料完成扫码登录、创建profile、精确唯一绑定指定联系人，并严格匹配本轮随机测试口令。只从目录游标后的 live/incoming/目标私聊事件读取一条文本进行核对，没有发送消息。

- contact_observation：event_count=3、target_incoming_count=1、target_live_count=1、text_checked=1、marker_matched=true。
- contact_result 为 ok=true/code=ok；login_result 与最外层 login_probe_finished 同为 ok=true/code=ok；SSH退出0。
- 已确认断开、停止原生进程、清理 client/helper/keeper/slice、TAP和临时资料；host_namespace_unchanged、host_network_unchanged、business_processes_unchanged 均为 true，IPv6 RA 自然计时变化另列。
- 流量观测出站205061、入站215465字节，仅计数、不宣称硬配额；128MiB/20%CPU/32tasks等限制不变。

目前登录、指定联系人识别和私聊接收三个基本技术假设均已实测成立。接下来准备向同一联系人发送一条固定文本的单次测试；具体文本先请用户确认，未获确认前不调用发送API。现有AstrBot和Docker/FRP保持原状；群收发和AstrBot桥接仍待完成。
