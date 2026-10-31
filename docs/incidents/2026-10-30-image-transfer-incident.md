# 2026-10-30 图片收发故障与恢复

用户报告微信消息不再进入 AstrBot，随后又报告“每次上传图片都会卡死”。本轮先恢复文本中转，再把图片的收发两条路径分别修到不再拖垮服务。所有操作都复用已在线微信会话，没有初始化、扫码或 fresh login。

## 一、中转停止

- `astrbot-wechat-forwarder.service` 为 failed。日志显示一次启动后正常运行约 43 分钟，随后 `status=1` 退出；此后三次自动重启都在约 2 秒内退出，触发 `StartLimitBurst` 后不再尝试。
- 该 unit 的 `StandardOutput` 与 `StandardError` 都是 `null`，应用层错误不进 journal，必须从私有状态目录取原因。
- 桥状态文件给出的启动原因码是 `previous_image_send_requires_review`，说明卡在图片审计，而不是登录、网络或 Native 会话。
- 同时刻 AstrBot 侧的最后一次入站消息早于桥退出；此后 AstrBot 对图片动作报 `WebSocket API call timeout`，这是桥不在线导致的结果，不是原因。
- Native 会话当时已连续运行约 21 小时，AstrBot 容器也长期在线，因此排除微信掉线和 AstrBot 故障。

## 二、图片发送结果未知

- 出问题的是一次约 900KiB 的表情图发送。桥侧的 live 审计为 `state=unknown`、`code=image_timeout`；旧的隔离审计（另一张约 27KiB 图片）仍然保持 `unknown`，与本次无关。
- Native 侧该作业为 `state=needs_review`、`phase=uploading`、`attempts=1`、`error_code=transport_unavailable`，`completedBytes=0`，没有服务器消息 ID，也没有 Native 回执，说明上传一个字节都没成功。
- 桥的超时语义把“本地作业已创建”当成“提交可能已经发生”，于是写 unknown 并停服。这是原设计里最脆的一环：一次上传失败会连带停掉所有文字转发。

## 三、恢复文本中转

1. 读取私有状态目录中的图片审计、hold 与冻结记录，逐字段核对 digest 与 Native 实例标识。
2. 用只读查询确认该作业从未进入 `submitting`、没有服务器消息 ID 和回执，属于可证明的未提交。
3. 先把原审计、hold、冻结记录和状态文件备份到私有目录 `review-<timestamp>/`，再把 live 审计标记为 `rejected`，并写独立 review 记录说明依据。旧的历史 unknown 审计与隔离模式原样保留，不清除。
4. 先 `image-output disable`，避免定位期间再次触发上传；`reset-failed` 后启动，约 90 秒内到达 `active/ready`。
5. 启动初期 `status` 仍显示旧原因码，属写状态与读状态之间的竞态，不是新故障。

AstrBot 随后重连反向 WebSocket，文本收发恢复。

## 四、图片输出不再停服

`scripts/image_send_native.py` 的失败判定改为按状态分流：

- `failed` 且阶段仍在 `preparing`、`uploading`、`readback`，或 `needs_review` 且阶段仍是 `preparing`、`uploading`，且没有服务器消息 ID 和 Native 回执 → 判为确定未提交，控制器写 `rejected` 审计并返回明确失败，桥继续工作。
- 其余情况（已进入 `submitting`、`readback` 之后，或已完成却没有回执）仍按结果未知处理，保留审计并停止桥等待核对。

单元测试覆盖了这两组阶段的分流。随后重新开启图片输出并做了一次真实发送：作业 `accepted/completed`，12KB，约 21 秒完成，回执与消息 ID 齐全，桥状态保持 `active/ready`，文字计数继续增长。同一窗口的 strace 记录到 4 次面向公网 443 的 `connect`，确认 CDN 上传路径确实可用；此前那次 0 字节失败没有复现。

## 五、图片输入：先被当成表情，再被超时误杀

- 用户报告“我发图片给机器人，机器人收不到，AstrBot 里也看不到”时，先核对了 Native 的消息类型：其中一次是 `type=47`（表情包），按项目范围直接跳过，不是图片。
- 之后的两张真实 `type=3` 照片暴露出两个独立问题。

### 5.1 原图没下载完就放弃

第一张照片的 original 资源失败（`transport_unavailable`，两次尝试），standard 资源已 `available`，但读取在 60 秒到期时抛出 `image_timeout`，而回退分支只接受 `image_download_failed`、`image_transport`、`image_oversize`，不含超时，于是整条消息被判不可用。

修复方向不是缩短等待，而是扩大窗口并允许回退：

- 原图读取期限 60 秒 → **75 秒**。
- `image_timeout` 纳入回退集合，超时后重新读取一次资源列表，标准图若已就绪就直接使用。
- 取图整体预算 60 秒 → **80 秒**，并对 `image_unavailable`、`image_timeout` 最多重试一次。

### 5.2 缩略图任务把原图挤到后面

改完预算后仍有图片要等 20 秒以上。查 Native 的 `media_jobs` 发现每张图片都会先入队一条**缩略图**下载作业，失败耗时 25–28 秒，期间 `completedBytes=0`；原图作业必须等它结束才能开始。

上游源码说明了原因：

- 自动下载判定对“图片 + 缩略图”无条件放行，`autoDownloadMaxBytes` 只作用于原图和标准图。所以把上限设为 0 也关不掉缩略图。
- 媒体作业使用**按账号的单一串行门**，一个 job 跑完才轮到下一个。缩略图空转 25 秒，原图就排 25 秒。
- 这些缩略图失败发生在取得 CDN 配置的阶段，还没发出 HTTP 请求，因此 `/proc/<pid>/net/tcp` 里看不到 443 连接，出口放行对它无效。

最终采取的做法：

1. 确认账号设置 `auto_download_max_bytes=0`，即 Native 不再自动排队下载原图和标准图；
2. 桥自己发起 `POST /messages/{id}/media-transfers` 申请原图，拿到 job ID 后**先**打开该作业的 CDN 出口，再轮询，消除“下载先于放行”的固定失败；
3. 临时出口按实际观察到的公网 IPv4:443 目标逐个放行，一张图可能轮换多个地址，因此允许一次任务内累积到 64 个，任务结束立即恢复原策略；
4. 只有原图被转发（保持既有策略），标准图仅在原图超时且已就绪时回退，缩略图始终不作为内容。

这项设置不在本仓库代码里，属于部署前提：迁移账号、恢复旧快照或重建 profile 后必须重新核对，否则会退回“每张图多等 20 秒以上”。

## 六、文字发送再次未决

调试期间另有一次 72 字节的私聊文字发送，Native 侧变成 `needs_review`、无服务器消息 ID、`error_code=submission_outcome_unknown`，桥按设计写 unknown 并停服。用户确认没收到该文字，因此走人工核对：

1. 新增 `scripts/text_send_recovery.py:cleared(audit)`：启动门接受 `state=accepted`，或 `state=rejected` 且 `review=reviewed_send_not_delivered`。
2. 核对 Native 侧确实不是 accepted 后，把该审计备份并标为 `rejected/reviewed_send_not_delivered`。
3. `self_proof` 要求存在已接受的发送样例，因此把最近一条真实 accepted 的审计复制为当前审计文件，原 rejected 记录留在备份目录。
4. 期间暴露一个既有缺陷：`serve()` 清理阶段引用了未初始化的 `queue_counts`，抛出 `UnboundLocalError` 并掩盖真实原因；补初始化后启动成功。

恢复后文本收发正常，未重发任何消息。

## 七、当前状态与遗留

- 文本中转、图片输入、图片输出均在运行；图片输入按原图优先、75 秒读取、80 秒总预算工作；图片发送失败不再停桥。
- 已知未完成：缩略图自动下载仍会在 Native 里排队并失败，只是不再阻塞原图；尚未找到在不改上游二进制的前提下彻底关闭它的办法。`tests/test_onebot_images.py::NativeMediaPolicy::test_thumbnail_job_is_bound_to_exact_asset` 是既有失败（在干净 HEAD 上同样失败），与本轮改动无关。
- 10-29 表情包发送返回 `1400 unsupported_message_segment` 的历史问题仍是另一条线，本次未闭环；本轮修复只保证这类失败不再拖垮中转。
