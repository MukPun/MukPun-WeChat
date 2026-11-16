# 发送隔离版本启动记录

用户在源码修复和139项受限合成验收后明确要求“启动桥”。授权涵盖切换已验收源码、重新建立覆盖现有第五项隔离的恢复清单，并仅启动桥；不重启Native、不重放未知发送、不增加测试消息或模型调用。沿用绑定私聊与现有五媒体开关，群关闭。

## 执行边界

上次native_changed失败回执保持原状并持久备份，不声称已解决根因。当前原Native在线、桥已停止，无需再次stop。以新的独立限定工具核对原owner/PID/ticks、所有worker/cgroup退出、基础媒体策略、完整审计/账本/队列/真实接收锚点和零私聊积压；不重跑旧一次性工具。互斥下备份状态、生成包含五项准入的新清单，只允许现有capture在指针提交后推进保护下界。journal不改写。

普通结果未知仍保持1408和幂等，所有历史例外不重发。完整证据归档后切换已审核提交、同步显式代码视图；原warm-only guard、Restart/通知/维护策略与配置保持。任何身份、证据或接收边界检查失败不启动。成功必须实际观察Native/SSE/WS、身份与ready，以及捕获自然结束，不能以systemctl start返回代替。

## 进度

- [x] 只读预检：原Native在线认证、剩余338547秒，queue2885 forwarded，无其他状态，54项未决；第五笔needs_review/submitting/13184字节无回执，ledger revision5；旧selected仅覆盖4项，真实接收锚点及零私聊积压通过。
- [x] 独立限定工具静态复审及拒绝路径验证：17项纯门控与3项真实SQLite/capture/Store.publish针对验证通过，复审发现及修正见下。
- [x] 持久备份、完整新清单、源码切换和单次启动。
- [x] 真实就绪核验和文档交接；发布审计结果见下。

本文件记录实际操作进度，后续结果继续追加；先前[发送隔离源码计划](2026-11-16-send-outcome-isolation.md)中的“未部署”是当轮历史结果。

## 实际执行

独立复审发现旧捕获工具的未执行分支把before_commit定义为零参数，现有Store实际传入manifest；新工具改为显式接收value，并核对boundary/revision及完整baseline。真实合成验证覆盖成功调用、证据变化拒绝及候选不符拒绝，后两者pointer和保护下界保持。另确认有1份legacy异常audit，没有内嵌recoveryDisposition；严格复用_legacy_cleared证明，将原audit、hold及.recovery.json逐字节备份并加入不变摘要，不合成标记或改写旧结果。只有已有新marker的异常进入新完整证据档。

实际互斥核验/持久备份后，捕获发布含5项准入的新清单，保持ledger revision5；正式源码从cc59696快进至99dc7d6，显式视图264文件、changed31/removed0。切换进程随后在sync_explicit_runtime阶段出现AttributeError，尚未start。源码核对发现：新checkpoint需要PublishDeferred，而同进程缓存的旧manifest没有该接口；原始工具仅保留异常类型，不能把源码推断写成已取得完整traceback。没有因此回滚清单、重跑apply或更改原审计。

后续增量工具经复审批准，仅用全新解释器从既有start-proof/capture-proof续接，完整重证30分钟窗口、原owner/Native、全部baseline、5项准入及新源码视图；没有再次capture/merge/syncapply。完整异常归档和legacy备份核对后，唯一启动标记落盘，原warm-only入口单次start成功。root私有send-outcome-isolation-start-20261116保存原失败回执、journal、账本、配置、guard、桥库、旧指针/manifest、审计和各阶段proof；不得重用apply/resume或把此例外泛化为后续启动许可。

最终现场就绪证明：状态年龄2秒，bridge/owner active，原Native/owner身份保持，SSE/WS/自身身份/ready均true，capture_phase=null、recovery=ready，捕获unit/PID/cgroup均闲置，无pending send review/outbound。queue2885 forwarded、历史未决54保持；新启动私聊/群的收转回计数均0，没有额外测试、重发或模型调用。privateOnly及五媒体开关true、有效群关闭；264文件运行视图changed0/removed0。该观察不证明长期稳定、物理重启或旧native_changed根因已解决。

开发/设计/进度/AI入口已同步；最终公开审计355文件、零发现，git diff --check通过。纯文档收尾不重跑已验收的功能测试，不再次重启桥；本轮本地提交用于精确部署，未推送GitHub。
