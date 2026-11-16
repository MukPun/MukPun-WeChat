# 私聊原生表情输入执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在原Native会话中启用绑定私聊的受限type47图片输入，并证明一张已有表情可以取图，不回放旧消息或额外调用模型。

**Architecture:** 复用Native read_sticker、既有媒体租约和图片IPC/文件交付。仅修正sticker期限与现有租约的配合、补有效覆盖，再在受审精确桥停止/暖启动路径更新配置和guard。

**Tech Stack:** Python 3.12、SQLite、Native HTTP/SSE、systemd现有隔离、OneBot v11。

## 全局约束

- 用户已于2026-11-16批准[设计](../specs/2026-11-16-private-sticker-input-design.md)，包括一次已有表情取图和仅重启桥；不再为同范围要求确认。
- privateOnly保持true、群/原生表情输出关闭；保留原Native/owner PID与ticks、完整profile、四项处置、历史53笔未决和真实接收锚点。
- 不重启Native/AstrBot/Docker/FRP，不扫码或恢复登录，不放宽来源、资源、IPC、图片及租约限制。
- 只验证一张已有表情的一次下载作业，沿用作业内有限重试；0条主动发送、0次额外模型调用，不自动补交旧表情。
- 沿用已有测试证据，只跑sticker/相邻图片输入的必要覆盖；开发/设计/接力文档同步实际状态。

## Task 1：期限及真实worker覆盖

**Files:** Modify `scripts/image_native.py`、`tests/test_onebot_sticker_probe.py`、`tests/test_onebot_native_forward.py`。

**Interfaces:** 消费现有`Client.read_sticker(identity, deadline)`及真实Native子进程夹具；输出同一图片payload/现有unavailable报告和checkpoint/ACK语义。

- [x] 添加跨预算回归：固定monotonic=100，pending原生sticker走真实`read_sticker`选择流程，捕获`_sticker_transfer` deadline；给定outer=1000必须≤160，outer=130不得延长。旧75秒会得到175，先记录预期失败。
- [x] 仅将`_read_sticker`的自身读取上限改为60秒，留出现有hd_input租约65秒的收尾余量，保持整体80秒、重试/请求/图片上限不变。
- [x] 扩展现有合成API/真实worker夹具，覆盖：私聊type47开启后原字节图片交付；关闭时单次跳过且不查媒体；不可用后ACK继续后续普通文本。消息绑定与群关闭沿用真实路径。
- [x] 运行`PYTHONPATH=tests python3 -B -m unittest test_onebot_sticker_probe`及新worker用例和一条普通图片相邻用例，不重跑全量；审查后提交。

Task1证据：`50c3077`，期限回归先失败175≠160；13项针对测试通过（3.206s），独立任务复审通过。未重复全量或调用模型。

## Task 2：一次原素材取图与限定启用

**Files:** 单次工具仅在`/tmp/`；实例配置/guard/证明仅root私有目录，均不入Git。显式角色清单已覆盖被修改的image_native，无新运行模块。

**Interfaces:** 原正式入口`run_forwarder.py`、固定guard、`Production.validate/inspect_owner/read/no_workers`、`PinnedNative`、`image_native.Client`与`media_egress.Lease`。

- [x] 准备精确工具，绑定旧/新源码HEAD、旧guard SHA和实际加载ExecStart、原Native accepted审计PID/ticks；只操作本项目精确bridge unit。静态复核后才执行。
- [x] 旧源码下确认fresh ready、无在途发送/媒体、无capture，停止桥；原方案要求clean completion。本次实际native_changed/okfalse，cleanup/原Native/宿主三基线均true。精确失败经独立工具复核、完整准入及no-worker/cgroup等证明并私有持久备份后接纳，未改journal，不声称clean stop。仅停止后改源，未绕过新review。
- [x] 原会话下完整selected/admission、GET身份/seed/锚点和积压检查，accepted无需额外CAS、所有审计accepted或已处置、未知53；保留数据库/账本/配置/审计摘要。
- [x] 精确定位绑定联系人最近那张type47，保存私有元数据与单次marker；用受限Client只允许该消息/资源、GET及单次media-transfers、同job有限retry/cancel，禁止send/login接口。作业收尾验证租约恢复及原Native，输出MIME/大小/完整性结果，不输出内容或URL。
- [x] 仅取图成功才快进已受审源码并同步显式视图；持久备份配置/guard/stop/probe证明后将stickerInputEnabled置true，guard能力断言改为五项true、groupContext false，其余Warm逻辑字节保持。源码/配置/审计/账本/队列绑定复核后一次暖启动桥。
- [x] 核新鲜ready、五能力、群关闭、原Native和capture结束；不回放既有表情，不新增主动真实测试。若任何证明失败，保留现场并完成未受阻工作，不重复下载或删记录。

## Task 3：交接与收尾

**Files:** `README.md`、`AGENTS.md`、`docs/DEVELOPMENT.md`、`docs/sticker-input-diagnosis.md`、`docs/private-canary-rollout.md`、本计划与设计、架构进度/交接的最新指针。

- [x] 写清用户批准、实际样本取图与格式、测试覆盖/失败尝试、部署与真实识图边界；不能将preview/GIF字节交付写成全帧动画理解。
- [x] 新guard摘要及旧工具失效范围记录在交接中，保留Restart/通知/维护策略与前一host未知故障限制。
- [x] 运行`git diff --check`、`python3 -B tools/audit_publication.py .`，检查无敏感资料；提交并仅同步本次公开代码/文档，报告实际状态。

Task2执行记录：受审工具最终SHA256 11a9ebd57a12c01dc11fda8d33b4f80304851c3bcdc74a95f018ce045c7c0107；精确adopt-stopped失败保留路径、probe和enable均已执行。一次早期probe在marker前被自有文档脏状态门禁拒绝，没有下载作业；整理后唯一下载成功（GIF14,327字节、attempts1、available/completed、原策略恢复）。正式源码50c3077与262文件视图已同步，仅stickerInput及guard能力门禁改变，原会话暖启动，最终健康见灰度末段。

最终核验：age1s、ready/Native/SSE/WS/identity=true，capture0/recoveryready，原Native/owner、privateOnly/五能力true、groupsfalse、2879 forwarded/旧未决53、262文件视图匹配、CDN策略恢复。新启动计数全0，无旧消息回放。较早捕获未完成时最终门禁未过，未调整门禁或重启，等自然收尾后通过。Task3文档与发布审计349文件0发现已完成，最终整体复审与提交同步记录在AI接力入口。

最终整体复审及健康补充复审通过，无阻塞发现。用户继续后的当前只读复核再次通过（状态age0s、capture0、原Native/owner、五能力/群关闭、2879 forwarded/旧未决53、262文件视图保持）；文档最终提交并同步正式副本，保留工作副本供后续接力，不push或额外重启。
