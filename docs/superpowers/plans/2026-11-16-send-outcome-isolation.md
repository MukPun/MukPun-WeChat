# 发送结果隔离实施计划

**目标：** 普通文本、图片和语音发送失败/未知只影响原动作，后续消息继续，日志可追踪。

**架构：** 保留 core 幂等和 Native 证据，移除普通未知结果对交付准入的控制；runtime 负责有界诊断、按请求串行和恢复。采用当前隔离工作树 codex/wechat-private-canary，无额外 worktree、无生产操作。

## 任务

- [x] 先修改/新增合成回归：旧实现确认3 failures/4 errors，包含普通未知关闭全局入口和旧媒体回执触发协议错误；首轮因直接导入TestCase而额外收集旧用例，已改为模块引用，不把47项都计作新增测试。
- [x] 修改 core/sending.py、adapters/wechat/proxy.py：删除自动全局 hold，保留显式保护门和原动作缓存；媒体增加有界退休请求/晚到处理，覆盖cancel到finally之间及晚到ACK，不放宽目标与摘要校验。
- [x] 修改 runtime/reports.py、session.py、output.py、state.py、broker.py 与 scripts/voice_forwarding.py：未知结果只进入诊断、移除普通待决对输出/激活/捕获的阻塞；忙请求单笔拒绝，旧审计保留；语音结果超时进入受监督恢复，保留旧作业证据。
- [x] 新增 runtime/outcomes.py 的固定字段 JSONL 诊断、跨进程锁及私有轮转；普通action拒绝和Native结果都记录。新增pending_send_outcomes的同Native非终态保存/固定快照结算和完整证据归档；源清单/受限runner已登记。
- [x] 复审修正：合法非终态不能抛通用恢复错误；failed/readback缺未提交证明降为unknown；正常ledger/pending追加只延后capture；最终指针CAS的PublishDeferred必须贯穿manifest包装并核对pointer未变；完整原audit+marker先归档再覆盖实时槽位/移除pending。
- [x] 补齐Native空闲管道上的媒体组装到期释放：新增真实子进程用例先因等不到结果而超时失败，修复后连同已有单图/分块并行回归3项2.399s通过；验证无POST、SSE继续和capture_pause释放。增量独立复审通过，无Critical/Important。
- [x] 发送/证据/runtime/打包定向123项19.185s通过；最后实际systemd受限139项通过、零跳过，八项隔离、资源限制、unit清理和原Native/宿主namespace/network/business保持全部true。模拟全部场景true。没有重跑完整项目全量。
- [x] 独立源码限定复审批准，无剩余Critical/Important；公开脱敏审计与最终文档检查结果见下。同步README、DEVELOPMENT、设计、进度与AGENTS，生产桥状态不由本地测试推断。

设计依据：[发送结果隔离](../specs/2026-11-16-send-outcome-isolation-design.md)。用户已在本次对话批准此方向，直接执行，无再次审批环节。

## 验证说明与交付边界

首轮194项通过后，复审补齐上述跨层路径。一次274项命令有1项旧断言仍期待PendingSend为ValueError，整体失败，不计全绿；后续123项及真实受限139项已覆盖修正后的发送/恢复路径。旧“未知即hold”和“证据不足必须抛错”断言改为验证新动作继续、原动作不重发、不能伪造未提交，错误绑定/损坏拒绝继续保留。受限137/138项为加入最后回归前的结果，不与最终139相加计算独立覆盖数。

发布审计曾标记日志脱敏测试里的虚构token文字；改为明确fixture前缀，保留不泄漏断言，不放宽审计规则。只改变测试占位值，不影响已验收的生产源码。随后迁移/日志补充17项5.629s通过；最终公开脱敏审计354文件、零发现，git diff --check通过，人工检查源码/文档差异。日志、完整证据和运行资料没有加入Git。

本轮未部署、未推送GitHub、未重启生产桥/Native、未发真实微信消息或调用模型；保留当前工作树。线上仍是旧版本在native_changed门控失败后暂停的状态，此缺陷不是本轮发送隔离已修复的内容。不得复用旧一次性暖恢复工具直接启动。
