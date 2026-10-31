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

- Linux 微信接入端源码固定 `agent-wechat v1.2.0`，经受限 IPC 接入 Python OneBot v11 桥，再通过本机反向 WebSocket 连接 AstrBot。部署与验收状态见 [Native 升级记录](docs/native-1.2.0-upgrade.md)，不能从源码版本推断线上已切换。
- 桥负责传输、稳定身份、路由、去重、防循环、受限图片交付和回执；模型、人格、上下文、主动回复及插件属于 AstrBot。
- 正式配置默认 `allGroupsEnabled=true`，缺少该字段的旧配置也默认开启。收到新群消息后建立稳定路由；不是启动时枚举全部微信服务器群目录。
- 全群普通文本逐条上报；开启图片输入时，支持的普通群图片也可直接上报。只保留真实原生 @，不把普通消息或 `/ai` 伪造成 @，不改写全群模式的命令正文。是否唤醒由 AstrBot 决定。
- 私聊仍只绑定指定联系人。不要将“全群”误读为“所有好友私聊”。旧单群模式仍保留前缀、原生 @ 及上下文开关。
- 已实现持续 SSE、持久队列、文本分段、普通图片输入、受限单张静态图片输出、成员昵称/群名片透传和会话保存恢复。图片由桥主动发起 `media-transfers` 下载原图，读取期限 75 秒、整体预算 80 秒；图片上传阶段的确定失败不再中断文本桥，只有提交阶段之后的结果未知才停桥待核对。
- 缓冲合并明确交给 AstrBot `chat_plus`，不在桥内重复开发。微信原生 `type=47` 表情包输入暂缓；WebUI 和网页配置编辑尚未实现。

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

根据改动运行相关测试，不为纯文档变更调用模型或重启服务。Windows 的 POSIX/systemd 用例会跳过；涉及 Linux 权限、管道、快照或隔离时，必须补对应 Linux 受限验证，不能用 Windows 通过替代。更多测试选择见开发文档。

跨层协议或文件清单变更要同步检查 Native、控制器、OneBot 及部署打包契约。仅在精确核对无不确定发送、完成所需检查后部署本项目；保留 Native 会话及已有审计，不自动执行旧恢复脚本。

## 后续开发方向

核心文本、上下文、防循环、普通图片输入、静态图片输出已有实际验收；全群路由与成员名称已有后续实现记录。尚需按用户下一次任务选择：长期稳定性与期限管理、媒体缓存和预算生命周期、安装/路径配置通用化、更多协议能力或 WebUI 设计。不要自行恢复表情包输入或缓冲合并计划，也不要把所有未实现功能当成本次授权。
