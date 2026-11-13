# 模块化架构：当前实现与 AI 接手

更新：2026-11-13。Tasks 1–5 已落地并通过独立复审；Task 6 已完成下述局部修正与交接，等待最终整体静态复审。源码未部署，不能据此推断线上版本或健康。[阶段进度](architecture-rewrite-progress.md) 保存提交与验证证据；[设计](superpowers/specs/2026-11-12-modular-architecture-design.md) 保存决策；[开发文档](DEVELOPMENT.md) 说明业务和运行约束。阶段记录中的“后续任务待实现”是当时状态，以当前进度表为准。

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

逻辑分层没有增加进程。保持已有 Native 会话、root 控制器及两个受限 worker。`Workers.native_options()` 只向 Native 子进程的匿名管道交付 Native token；`spawn_host()` 只含 OneBot token 与非秘密路由/握手配置。host 只挂载公开代码/依赖与桥状态，Native 的 `/var/lib/astrbot-wechat-session`、`/run/astrbot-wechat-session` 被屏蔽，没有 profile/会话挂载。公开代码里存在 runtime/adapter 定义不代表 host 取得凭据或部署权限。资源、网络、只读挂载、所有权和媒体许可限制保持。

`PUBLIC_CODE` 是公开源码交付清单，`NATIVE_SOURCES` 是 flat 角色目标→源映射，host 使用嵌套包角色清单。`sync_runtime` 与隔离恢复验收 `stage_tree` 均调用同一 `public_sources`/路径校验，排除未登记暂存、私有目录、符号链接及不完整源码。生产 controller-host 在 ready 前核对 protocol=2、完整 host 角色源码 SHA-256 和摘要绑定 `AWG-READY/2`；混合/旧视图拒绝，不把整个 Native 通道宣称为已版本化。

恢复保持同代错误+退出只计一次、10 秒窗口第三次故障预算；只有明确进程/连接/就绪失败可重建。必须确认旧精确 unit/cgroup 停止，持久保存未知发送并退休 publishing，再关闭媒体和旧 capture，重新建立每代 relay/请求/ACK/握手。任一步失败不继续 spawn。真实 SSE 和 WS 连接、身份及锚点证明齐全才激活；`requires_review` / `capacity_requires_review` 同时阻止换代激活和已运行代投递，正式入口写盘阻塞并退出，最终状态也保留审查真值。空队列不能清除审查。

快照仍在单线程无请求/无媒体传输的安全点同步捕获。未知发送/锚点冲突保留审计，不重发、不重投模型、不跳到最新、不 fresh login。SQLite schema、持久数字 ID、echo/错误形状、序列化 IPC、动作集合和媒体预算未变。健康证明保持 75 秒：online 表示真实账号证明，good/ready 另要求 SSE/WS/恢复准入，过期或未知为 false。

## NapCat 采用点与微信差异

参考是本地源码快照 `/home/mukpun/game/.references/NapCatQQ`，不是本仓库依赖；`.reference.json` 固定 `72912f293924f43463b5976b01fc094a8ae11481`（归档 SHA-256 `8bc74be5caba81edfb9f2fd2624597b14a6e681f9d005f21e1d57bde688b59f2`）。其他 clone 没有该目录时，使用同提交的 [NapCatQQ 源码](https://github.com/NapNeko/NapCatQQ/tree/72912f293924f43463b5976b01fc094a8ae11481) 核对职责，不将快照/二进制提交进本仓库。

| 固定源码 | 采用 | 保留的微信差异 |
| --- | --- | --- |
| `packages/napcat-core/index.ts`、`apis/msg.ts`、`helper/event.ts` | API 聚合、委托底层原生客户端、绑定异步完成 | 不把 session/core 凭据传给 host；HTTP 提交一次，SSE 先持久 ACK；超时不证明未发送，仍要求作业/回执投影。 |
| `packages/napcat-onebot/action/index.ts`、`OneBotAction.ts`、`event/message/*`、`index.ts` | 显式动作注册、统一校验/响应、事件转换分工 | 不引入 QQ 数字身份、TypeBox coercion、动作别名/流；保持 SQLite 映射、严格整链校验、echo 与未知发送语义。 |
| `packages/napcat-onebot/network/index.ts`、`adapter.ts`、`websocket-client.ts`、`config/config.ts`、`action/system/GetStatus.ts` | 网络生命周期、事件源、协议服务和账号状态分离 | 保留一个反向 WS、严格配置与资源预算；不增加 HTTP、多传输、热重载、JSON5 或乐观 online/good 默认。 |
| `packages/napcat-shell/napcat.ts` | worker 所有权、主动停止排除、10 秒/第三次崩溃监督 | 保留 root/两 worker 与完整 Native 会话，必须证明停止和持久退休；不快速登录、不 best-effort 停止后继续启动、不宽泛杀进程树。 |

## 验证证据与限制

实现提交：基础 `d460163` / `9f67d4b`，微信适配 `965699b`，OneBot `47e4a9d` / `44cf087`，transport/health `f1aeb71`，runtime `27a0fa4`，审查修复 `0452ba7`。Task 5 独立静态复审批准记录为 `3213311`；最终整体复审仍待执行。

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

先运行 `git status --short --branch`，读 AGENTS、当前模块表和阶段进度。不要重新实施 Tasks 1–5，也不要因纯文档/静态复审重复全量测试。最终审查范围为原始基线到交付提交的正式入口、依赖方向、角色清单/凭据边界、旧状态兼容与恢复保护；修复真实发现后只跑对应有效覆盖。新源码必须登记 PUBLIC_CODE，Native flat helper 同时登记映射，新增专题须登记受限 runner。

当前剩余：整体静态复审及其真实发现；有权限时另行运行保持既有预算的 isolated 合成验收。生产部署、push/merge、真实宿主重启/扫码/消息/模型以及 Docker/AstrBot/FRP/网络操作均未授权。后续功能方向需用户另行指定，不自行增加表情包、缓冲合并、WebUI、SDK 升级或通用安装器。
