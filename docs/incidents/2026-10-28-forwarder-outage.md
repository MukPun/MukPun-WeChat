# 2026-10-28 中转停止与核心恢复

用户反馈微信消息无回复，并提供北京时间12:29:51 AstrBot v4.29.3 `ApiNotAvailable`。本轮优先恢复核心转发，未推进图片扩展。

## 确认的原因

- 检查时 `astrbot-wechat-forwarder.service` 为 failed，自动重启计数4；微信会话 owner 仍 active。
- 中转最新启动错误为 `previous_send_requires_review`。最后一条文本发送审计约12:26:41留下 `unknown`，后续启动因该保护被拒绝。
- 桥队列596条均为forwarded，没有queued、publishing或uncertain；动作表有一条尚未写入结果。
- 新鲜保存的Native快照中，该发送为accepted，幂等键、目标摘要均与审计一致。随后用独立受限GET检查确认服务器回执及同目标的文本投影，正文SHA256和字节数一致。
- 因此该条消息已被微信接受，桥未完成本地结果记录。桥停止导致AstrBot没有可用的OneBot反向连接，与用户的ApiNotAvailable相符。

最初为何未完成本地结果记录仍未确定。故障时间窗没有取得可用的systemd生命周期记录，不能断言为超时、进程崩溃或OOM。恢复后Native父组memory.events显示oom/oom_kill/max均为0。

## 恢复动作

1. 复用已在线微信会话，保存新鲜快照；不初始化、不扫码。
2. 在现有资源边界内启动短时GET检查进程，只读取该send和消息投影，没有发送POST。
3. 将原unknown审计及原动作记录备份至项目外root私有目录 `outage-recovery-<timestamp>/`。
4. 仅核对并修正这一条动作结果、出站消息映射和审计为已接受；未清空数据库、队列或游标，未重发消息。
5. 清理本项目failed状态并启动本项目中转，保留Native登录进程。

检查期间宿主网络/命名空间及Docker/FRP业务进程基线一致；未执行Docker/FRP配置、重启、网络或宿主内核操作。

## 恢复后验证

北京时间约12:48，active/ready、SSE、AstrBot连接均正常，状态更新时间距检查1秒；all_groups_enabled保持true，未生成二维码。

新实例收到22条并转发22条，完成3次回复，私聊和群聊均有流量；队列仅有618条forwarded元数据，无待处理或不确定记录，OneBot连接错误0。控制器、OneBot、Native父组OOM计数均为0，资源上限保持。

这些是实际到达的消息，不是新增的人工模型验收测试。尚未完成长期稳定性验收；原始结果未落盘的触发点仍需补充有界、无正文的退出元数据来追踪。不得为保证自动恢复而直接把任意unknown标为成功，或重放未核对的发送。

辅助工具：`review_forwarder_outage.py`默认只读；`--recover`仅适用于本轮已接受、单段且恰好匹配的未决文本发送，带严格核验、备份和不重发约束，不是通用强制解锁工具。`inspect_forwarder_outage_reason.py`读取健康状态及固定时间窗的管理器元数据。
