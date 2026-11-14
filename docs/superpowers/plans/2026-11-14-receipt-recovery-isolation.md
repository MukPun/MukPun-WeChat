# 回执与捕获隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复已交付却记未知、慢捕获阻塞IPC及普通动作错误拖停全桥，保留单次提交和恢复证明。

**Architecture:** 动作层保存幂等结果，Wechat适配器绑定正常/晚到Native证据，runtime以显式阶段机管理捕获和待核对状态。大文件证明采用私有固定副本和有界作业；epoch/代际/真实锚点/清单指针提交保持因果约束。

**Tech Stack:** Python 3.12、SQLite、匿名管道、systemd、OneBot v11；不增加依赖。

## Global Constraints

- 仅现有绑定私聊，群媒体关闭；不追加真实消息、模型调用、扫码、initialize、logout或会话替换。
- 保留所有动作、身份、队列、审计和四项Native处置，不重发/重放已处理消息。
- Native快照与bridge副本在受证明的同一静止窗口固定，指针提交之后才可推进两代最小下界。
- 大文件IO不进入常驻转发主循环；只复用同次操作中固定且身份未变化的校验，不跨重启信任缓存。
- 仅操作本项目，不动Docker/AstrBot/FRP或宿主网络；新模块登记PUBLIC_CODE/角色/runner，私有资料不进入OneBot。
- 用户已批准方案及实施，执行中不再逐步请求相同授权；真实额外发送和群开放不在范围。

### Task 1: 已接受审计与未知桥动作对账

**Files:** `scripts/text_send_recovery.py`、`tests/test_text_send_recovery.py`；启动接入由集成任务修改`run_private_forwarder.py`。

**Interfaces:** `needs_reconciliation(core, audit_path, settings) -> bool`只读检查当前accepted审计是否唯一匹配NULL/1408短文本动作；`reconcile(core, audit_path, settings) -> bool`保留原签名，接受submitted/unknown/accepted审计，严格GET与原实例证明后只修正匹配行。旧式accepted审计没有完整绑定时不擅自修改；不新建Native发送。

- [x] 在现有RecoveryTests增加accepted审计+1408且其他行/图片语音不变、重复对账不重复写结果、错误proof/代际/分段拒绝的用例。RED命令：`PYTHONPATH=tests python3 -B -m unittest test_text_send_recovery -q`。
- [x] 实现上述两个接口，复用当前backup/readback/unknown_result/CAS事务；accepted不等于跳过GET，成功后同一outgoing身份和ret0结果幂等。
- [x] 同一专题GREEN，记录测试数/时间、差异和限制到本次SDD报告，提交仅自己的文件。

### Task 2: 文本晚到回执与动作层普通失败

**Files:** `wechat_bridge/adapters/wechat/proxy.py`、`wechat_bridge/core/sending.py`、`wechat_bridge/storage/state.py`、`scripts/forward_host_worker.py`及对应专题测试。

**Interfaces:** proxy对已超时文本保留最多32条/300秒的绑定，未知requestId仍拒绝；`set_late_text_handler(handler)`在DeliveryService初始化时可选注册。handler接收`(part_key, destination, Receipt)`，只为单分段、账号/目标/accepted证明完整且匹配原NULL/1408状态的动作做CAS，返回是否已核对。多段不自动报整体成功。normal/late处理均不重发、不调用模型、不补发WS答复。

- [x] 先以真实Spool、短测试等待、合成IPC验证超时后accepted回执、错误ID/目标、重复回执、过期/容量和多分段拒绝，证明旧代码失败；在现有套件或新增专门套件中落实。
- [x] 新增存储`finish_unknown(key, digest, native_key, result) -> bool`事务CAS：仅NULL/规范1408变成同一outgoing成功；冲突/其他结果不覆盖。handler只使用持久绑定及平台accepted策略，不记录正文到诊断。
- [x] `action_result`把确定的1400/1403普通拒绝与1408未知区分；与runtime约定保留`action_rejected`用于待核对，新增`action_failed`固定retcode用于普通失败。新增`late_text_receipt`报告仅携带固定结果、requestId、part_key、destination和send_id，不含正文；runtime核对本轮root审计后处理，不直接信任worker结论解保护。
- [x] 定向验证proxy/core/storage/action_result及现有相邻文本/图片回执；登记新测试到显式runner/清单由集成任务统一处理。报告与提交自己的文件，不修改runtime控制器或公共文档。

### Task 3: 捕获阶段拆分与故障隔离

**Files:** `scripts/recovery_checkpoint.py`、`scripts/recovery_manifest.py`、`wechat_bridge/adapters/wechat/recovery.py`、`wechat_bridge/runtime/{controller,state,session,reports,broker,processes}.py`、`scripts/run_private_forwarder.py`；新增有界捕获作业模块及角色登记按设计实现。

**Interfaces:** `RecoveryBackend.capture_if_safe()`保持正式入口但只推进非阻塞阶段；`close()`退出时回收精确作业。捕获token绑定Native identity/epoch/profile/worker代际；host暂停入场握手、固定原始pair后恢复收发，作业仅处理私有固定副本。`Store.pairs()`在同次锁定中返回已验证current/previous，不能取消copy后验证或把缓存作为下一次启动证明。

- [x] 本地合成慢捕获阻塞证明与动作到达，先复现主循环不能处理IPC的旧缺陷；加入阶段/取消/代际/指针提交中断/下界检查及真实进程夹具，避免真实发送和模型。
- [x] 拆分固定输入、耗时证明/发布与主循环应用完成结果，静止窗口内不接纳新的发送；主循环在等待时处理已有请求、IPC、健康与退出。完整性失败保持保护，锁忙延后有界任务，不伪造ready。
- [x] 普通`action_failed`验证固定code后仅计数；未知动作进入可诊断待核对状态并关交付/新发送门，不能继续调用模型。完整性/协议/身份故障仍停桥。晚到结果需绑定root审计并完整核对，未核对则保留保护。
- [x] 接入Task1启动对账、Task2晚到回执；清理作业早于释放pinned/bridge锁，服务和worker中断不遗留后台发布者。
- [x] 运行受影响capture/manifest/runtime/协议/late专题，恢复相关额外migration/lifecycle及Linux isolated；不重复全量。

### Task 4: 文档、复审和受控恢复

**Files:** AGENTS、DEVELOPMENT、当前架构交接/进度、灰度记录、本设计/计划、显式清单/runner，以及限定部署入口。

- [x] 对照本地NapCat Action/Network实际源码核对职责，记录采用和保留的差异；同步上述开发与设计文档的实际实现、测试和剩余限制。
- [x] `git diff --check`、`python3 -B tools/audit_publication.py .`，独立任务与整体代码复审，修复真实发现并只补相关覆盖。
- [ ] 使用精确受审工具核对现场Native仍同实例、私信配置、清单/审计/队列和无额外绑定私信积压；仅GET对账本次已交付动作，保留旧历史行。
- [ ] 条件通过后同步正式副本/显式视图、恢复正式入口并只起私信桥；无新增模型调用。新鲜SSE/WS/Native/身份/ready和未知数变化逐项核对。条件不通过则保留停止与精确阻塞，不能宣称上线成功。
- [ ] 记录最终源码提交、工作树、部署状态、临时策略与下一位AI起点。


## 执行记录（2026-11-14）

Task1/2源码与专题覆盖已完成；Task3已接入正式异步阶段和握手、取消/提交CAS、普通动作隔离及模拟子进程。源码与文档仍未部署。实现/审查子agent均遇额度限制，root继续实现并静态核对；后续独立审查已修正两项Important并批准，旧SDD task报告不属于本次修复。详细验证与未完边界见灰度最新章节；发布前保留完整差异和源码审计。Task4真实对账/部署/恢复策略尚未执行。

最终收尾102项2.650s、真实systemd isolated119项通过，零跳过，cleanup与Native/业务基线通过；发布审计345文件、0发现，差异检查通过。后续真实对账和桥恢复仍需独立记录实际结果。
