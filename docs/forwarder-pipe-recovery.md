# 图片管道传输超时与中转恢复

## 故障与证据

2026-10-28 排查 AstrBot 收不到消息时，正式中转 unit 已失败并触发 systemd 启动频率限制；AstrBot 反向 WebSocket 端口仍监听，Native 会话仍在线。

图片审计停在 `submitted`，没有 `jobId`。首次恢复后，文本及两次图片回复成功，但约 1.8MiB 的图片再次导致 `image_transfer_binding`。该错误来自 Native 工作进程对分块组装 40 秒期限的检查。故障后、同一会话的完整快照中，没有该幂等键对应的媒体发送作业或文本发送记录。

正式控制器 `Duplex.drain()` 每轮只读取 4096 字节。一个 64KiB 原始图片块编码后需要多轮控制器调度才能组成完整 IPC 帧；在现有 CPU 限制下，大图可能先耗尽组装期限。修复将单次读取提高至有界的 65536 字节，减少轮询次数。

当时保留了 40 秒组装期限、256KiB 单帧、512KiB 聚合缓冲、逐块 ACK、完整图片校验，以及既有 CPU、内存和上传预算；未增加发送重试或放宽未知结果保护。

## 2026-11-01 期限重估

同一故障再次出现，这次是 4,409,578 字节的图片。40 秒期限对块数的关系是错的：图片按 64KiB 串行分块、每块一次往返 ACK，4.4MiB 需要 68 个往返，在 `CPUQuota=5%`、`Nice=19` 且宿主磁盘报 `BadCRC`/`hard reset` 时必然超期。超期又会抛出 `image_transfer_binding` 直接终止工作进程，控制器侧的 `native_image_result` 永远不会到达，审计就永久停在 `submitted`，下次启动被 `image_send_preflight` 拒绝，三次之后击穿 `StartLimitBurst`。

修复分两处，不改变"结果未知就不自动重发"的边界：

- 组装期限改为随载荷增长：`wechat_bridge/image_output.py` 的 `assembly_window()` 为 `20 + ceil(bytes/64KiB) × 2` 秒，上限 300 秒。`transfer_window()` 在其上再加 85 秒提交预算与 15 秒余量，同时用于 `wechat_bridge/forward_gateway.py` 的媒体传输上限和 `wechat_bridge/continuous.py` 的等待上限，避免外层先于内层超时。
- 提交预算与组装预算分离：`scripts/forward_native_worker.py` 的 `submit_deadline()` 只给发送阶段 `SUBMIT_SECONDS = 85` 秒，因为 `scripts/image_send_native.py` 的 `MAX_SECONDS = 90` 拒绝更长的剩余时间。组装快完成也不会把发送阶段挤到负值。
- 超期不再终止进程：分块分支先按 `requestId` 静默丢弃已放弃传输的迟到帧（绝不回 `chunk_ack`，否则控制器侧 `image_chunk_ack_binding` 会再炸），再对纯超期调用 `timeout_result()` 发出 `native_image_result`/`native_voice_result`，`submitted=False`、`server_confirmed=False`。控制器既有的未提交分支据此把审计写成 `rejected` 并放行启动。协议不匹配（分块类型或 `requestId` 与当前传输不符）仍然抛 `image_transfer_binding`。

验证：`tests/test_onebot_image_output.py::TransferWindows` 约束窗口随块数增长、提交预算低于 90 秒、4.4MiB 载荷拿到的窗口远超 40 秒；`tests/test_onebot_native_forward.py::MediaLockDeadlines` 约束超期结果的结构与提交预算不会越过组装截止。

## 2026-11-02 未确认结果不再终止中转

图片审计有一种永久卡死的组合：审计先被无条件写成 `submitted`，而唯一能推进终态的是工作进程回报的 `native_image_result`。若工作进程在回报前就退出（进程崩溃、IPC 断裂、上传期限内没等到回执），这条记录永远是 `submitted`，`image_send_preflight()` 又只接受 `accepted`/`rejected`，于是每次启动立即失败，三次后击穿 `StartLimitBurst`，OneBot 反向 WS 无人连接，AstrBot 侧表现为 `ApiNotAvailable` 或 `WebSocket API call timeout`。

实际发送结果与这条记录无关：`media_jobs` 中 `state='accepted' and kind='send'` 的 120 个作业全部带 `serverMessageId`，而三张卡住的图 `serverMessageId` 全为 `null`、`nativeReceipt` 全为 `null`。所以"卡住"既不等于"没送达"，也不等于"送达了"，只等于"本机没有确认"。

修复不改变"未知结果不自动重发"的边界，只改变它的处置方式：

- `scripts/run_private_forwarder.py` 新增 `record_image_review()`，把这样一张图登记成 `state='rejected'` 加 `outcome='unconfirmed'`，并追加到 `forward-image-review.json`（最多 32 条）。审计因此能通过下次启动的检查，而真实状态仍然可查。
- `native_image_result` 分支不再用 `require(accepted or unsubmitted, 'image_send_requires_review')` 结束整条中转。结果未确认时改为登记并继续，只更新 `last_image_error`。
- `native_error`/`host_error` 若发生在图片发送期间，也登记同一类记录，避免崩溃留下永久卡死的审计。其他工作进程错误仍然终止中转，因为那说明管道本身已不可信。
- 状态增加 `image_reviews` 与 `last_image_review_at`，让这类静默降级在中转状态里可见，而不是只留在日志外。

多张卡在图分别有独立证据：`job_fa64a111`（`submitting`/`media_processing_failed`）、`job_9feb5c8d`（`uploading`/`transport_unavailable`）、`job_8fa16662`（`submitting`/`media_processing_failed`）的 `serverMessageId`、`nativeReceipt` 与 `messageIds` 均为空；`job_6c8438a9` 与 `job_e41d215f` 则相反，`nativeReceipt.status == 'server_accepted'`、`profileProjection == 'complete'`、`uploadReadbackVerified is True`，说明它们实际已送达而本机丢了轮询响应。两类都不应阻塞中转。

## 恢复边界

仅在确认故障后的快照属于当前 Native 会话、没有匹配发送作业、没有 Native 待发任务、桥队列无待处理或不确定输入，且唯一未决动作匹配图片审计后，才将该动作记为明确失败。

修改前在实例私有状态目录备份审计、状态、原动作及完整桥数据库，并保存核对证据；保留原快照、幂等记录和上传预算。未重发失败图片。只启动本项目中转，复用既有 Native 会话；核对 Docker/FRP、宿主网络和会话身份基线保持一致。

## 验证范围

- 新增合成分块 IPC 预算回归：旧版耗尽预算失败，修复后通过。该测试模拟有限调度机会，不等同于生产耗时测量。
- 本机完整 OneBot 回归 257 项通过，含 Native 真实子进程、匿名管道、本地模拟 API 和 8MiB 分块。
- Linux 受限回环沙箱 292 项通过、零跳过；非 root、只读代码、零 capabilities、私有网络、资源限制等 8 项隔离检查通过。
- 部署后实际状态为 `ready=true`、`sse=true`、`astrbot_connected=true`、`uncertain=0`，保持原 Native 会话。

未为本次验证额外发送微信测试消息或调用真实模型。首次恢复期间自然产生的成功交付，不代表修复后约 1.8MiB 图片已完成真实发送验收；后续以自然交付及当前状态继续核对。
