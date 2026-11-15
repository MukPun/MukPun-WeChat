# 私聊媒体能力启用与捕获停止顺序

用户在北京时间2026-11-15明确要求开启图片收发、语音输出和链接卡片处理。启用现有实现，不扩展协议；`privateOnly=true`继续覆盖群配置，原生表情和群上下文保持关闭。不额外主动发送测试内容或调用模型，不重启Native、不恢复登录、不改AstrBot/Docker/FRP。

## 配置与会话边界

仅将`imageInputEnabled`、`imageOutputEnabled`、`voiceOutputEnabled`、`linkInputEnabled`改为true。配置不是热更新，需要重启桥。现有root私有warm-only入口仅修改这组能力断言；原owner/PID/ticks、GET在线和自身身份证明、拒绝prepare/launch等约束保留。备份原配置、入口、覆盖文件及必要的恢复journal，保留旧队列、身份、发送审计和四项隔离处置。53笔历史桥未决不清除、不重发。

启用前确认原Native在线认证、桥无待发/未核对请求、既有媒体审计accepted或精确处置、媒体容量可用，并核对`auto_download_max_bytes=0`。宿主SQLite无法解析guest数据目录的proc别名时，从既有静止快照仅复制registry及WAL/journal，逐文件与live fd字节摘要相等后查询私有临时副本；不打开或修改live SQLite，不生成新快照。

图片输出继续限单张静态图，语音继续依赖AstrBot提供合规record/WAV；链接卡片作为标题、摘要和链接等文本上报，不承诺完整网页抓取或原生卡片输出。保留现有媒体预算、提交未知保护和发送幂等。

## 开启前暴露的停止顺序问题

首次精确`systemctl stop`时四个开关尚未写入。manager事件证明捕获子unit先stopping/stopped，约数毫秒后才向父桥发起stopping；父桥误把子退出归为捕获异常，journal转requires_review。结束回执cleanup_ok/native_session_unchanged为true、uncertain为0，历史未决仍53，Native在线。不能将该结果当作新增未知发送，也不能盲目删除journal。

原`BindsTo+After`让systemd反向停止时先停子作业。仅反转为`Before`也不完整：父close同步等待子stop，而子stop等待父完成，真实合成unit已复现缺少parent_clean。最终保留`BindsTo`并改`Before`，父先收到SIGTERM；close先EOF撤销提交权，再向确切子unit发SIGTERM，排入`stop --no-block`，有界检查inactive/failed且MainPID0或明确not-found，并证明原cgroup为空。只有全部清理成功才置closed；重复close幂等。构造失败也走同一清理方式。其他unit默认同步stop不变，100秒握手、300秒作业和43/45秒发送期限不变。

此处沿用NapCat的任务归属与独立生命周期思想，保留微信的真实身份、root私有资料、持久回执和最终提交撤销约束。

## 核验与上线

定向回归先复现依赖断言及重复close错误，修复后capture/stop/lifecycle专题89项通过。真实systemd合成父子unit已证明parent_stop→child_stop→parent_clean，零Native操作、零消息；早期仅Before的失败不能记为通过。受限清理和启用后真实就绪结果随后写入[灰度记录](../../private-canary-rollout.md)。不重复1232项全量，不以配置开启代替真实媒体交付验收。

针对本次停止产生的精确review，须在停止、互斥和原owner绑定下重新通过选中清单/四项处置准入、真实GET身份和持久接收锚点、无rebase barrier及桥库摘要保持证明，备份原journal后才归档该review。下一次仍由warm-only正式启动重新完成ready；不修改发送动作、处置账本或接收游标，不授予任何冷恢复或重发许可。

## 最终实施状态

`37ff509`已部署，四能力开关已在运行状态生效，群关闭，桥自然经过首轮copying窗口后恢复新鲜ready，原Native在线认证且PID/ticks不变。完整备份的文件、backup目录及STATE父目录先持久，再改配置或归档review；manager已加载ExecStart在预检/归档前/实际启动前三处严格限定单一原warm-only命令，不能只检查磁盘某个drop-in。历史53笔未决、四项Native隔离和原接收边界保留，未新增真实消息/模型调用。

89项专题及真实systemd三场景通过，后者包含子提前正常退出、失败退出及重复close，证明实际终止而不等待被父排序阻塞的stop job。独立复审通过源码；工具最后补充的实际ExecStart断言由实际预检和主AI静态复核验证，未获得后续独立最终批准（审查agent额度耗尽）。本次是既有能力启用与运行验证，不代替真实媒体交付、语音接收播放或长期运行验收。

## 2026-11-16 既有链接输入恢复边界

用户已确认图片和语音正常。链接卡片实际Native投影为type49/app5/link与687字节`[Link]`文本，现有解析器适用；该卡片到达前桥已因host exit1/worker_failed停止，原微信仍在线。此次不变更消息类型或协议：精确旧worker退出、原owner/Native绑定、完整四项准入、GET身份/真实锚点和两条未消费输入证明后，持久保留失败回执、live journal/配置和账本不变，再通过原guard仅暖启动桥。只读needs_reconciliation=false断言排除额外对账；新旧53笔历史边界不授予重发许可。

实际原两条输入已received/forwarded各2，捕获结束后新鲜ready且原Native不变，已有用户流量正常触发回复；未主动生成新测试或重复投递。此为服务恢复与已有卡片交接验证，host原始故障、网页全文理解与长期稳定性仍无新增结论。细节见[分享卡片专题](../../link-input.md)和[灰度末段](../../private-canary-rollout.md)。
