# 私信灰度实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将模块化重构切换到仅指定私聊的正式灰度，等待用户一条短文本完成首轮。

**Architecture:** 配置 privateOnly 独立于 allGroupsEnabled；正式 Native/host 的持久检查点和 activate 不变。IPC、Native、协议三处约束目标，旧身份证明只读复用。

**Tech Stack:** Python 3.12、SQLite、OneBot v11、systemd。

## Global Constraints

只允许现有绑定联系人；群收发全部关闭；用户发起 1 条短文本，最多 1 次简短模型回复；图片/语音留到下一轮。不重启 Native、Docker、AstrBot、FRP；不删除会话、审计或队列。

### Task 1: 正式私信模式

修改 forward_config、Wechat assembly/native、host worker、runtime processes/broker 与 controller 装配。测试加入现有 routing/transport_health/runtime_lifecycle/native_forward 套件，保持显式交付清单不变。

- [x] 先增加 privateOnly 配置兼容、正式 host 身份与锚点/activate、已映射群 action 拒绝、控制器群帧拒绝，以及真实 Native 子进程私信模式用例。
- [x] `PYTHONPATH=tests python3.12 -B -m unittest test_onebot_routing.ConfigAndRecovery test_transport_health.PrivateOnlyHost test_runtime_lifecycle.PrivateOnlyRuntime test_onebot_native_forward.NativeRoundTrip.test_formal_private_only_worker_preserves_private_delivery -q`，确认新增边界未实现而失败。
- [x] 实现 `DEFAULT['privateOnly']=False`，校验严格 bool；Workers.native_options/spawn_host 传 privateOnly 并计算有效 allGroups；controller 不绑定群；formal host 以 selfSenderId 判断正式配置并保留锚点与激活；Native 在 privateOnly 时只允许 roomId=None/allGroups=false，历史群证明仍完整核验；broker 拒绝非 peer 的目标帧。
- [x] 同一命令通过后，仅运行受影响现有套件、`python3.12 -B tools/audit_publication.py .` 和 `git diff --check`，不重跑全量。
- [x] 同步 DEVELOPMENT、架构设计/交接/进度、README 和本计划实况；提交明确文件。

### Task 2: 受控部署与真实首轮

- [x] 只读 root 核对失败状态、Native 租期/在线性、审计、队列和当前 unit/source 身份，不打印正文、联系人或 token。
- [ ] 备份配置/unit 与旧源码版本；主副本无跟踪修改且可 fast-forward 时更新，保留未跟踪私有目录；sync_runtime 显式代码视图。
- [ ] 私有配置设 privateOnly=true 并关闭图片/语音/链接/上下文，核对 own unit 模板后只启动 forwarder。遇未知发送、审查阻塞或身份/锚点失败立即保留状态并报告，不自动重发或换登录。
- [ ] 确认 SSE、WS 和正式 ready 后通知用户发送短文本；只核对一次交付与一次回复的脱敏证据，再记录实际结果。群开放需后续授权。
