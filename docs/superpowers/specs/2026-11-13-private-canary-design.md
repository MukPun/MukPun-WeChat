# 私信灰度设计（2026-11-13）

用户已批准：使用现有绑定联系人，关闭所有群收发；用户发起一条短文本，最多一次简短模型回复，图片/语音留到下一轮。

正式配置增加 `privateOnly: bool = false`，旧配置行为保持。开启后覆盖全群和单群路由，不查找单群绑定；Native 和 host 配置显式携带同名开关且有效 allGroups=false。正式私信仍校验 selfProof、游标锚点、恢复清单和控制器 activate，不退回旧私聊探针。历史群发送只可作为只读 selfProof，仍核对账号、接受状态、投影、目标和正文摘要；不会授予该群发送权。

Native 拒绝群发送、以 checkpoint 跳过群事件；host gateway/core 的准入只含绑定联系人，群 action（包括 send_msg 推断）拒绝；控制器另在 IPC 路由处约束所有带目标的帧。旧队列不删除、不跳游标；若旧待决群消息或未知发送阻塞，则停止部署并说明。首轮关闭图片、语音、链接输入和上下文开关。

部署仅触及本项目源码、显式代码视图、私有配置及 forwarder unit，保留 Native/会话/审计/身份数据库。不改 Docker、AstrBot、FRP 和宿主网络。旧主副本私有未跟踪目录不得复制或删除。实际 root 预检、失败原因、剩余租期与审计核对通过后才能启动；sudo 当前要求密码，禁止把凭据发到聊天。

复用已记录的 1232 项全量验收，只运行本次配置、装配、IPC 和 Native 子进程相关用例及发布审计。合成测试与正式部署结果分开记录。

首次灰度的控制器重试用精确 /run drop-in 设 Restart=no；失败通知通过校验并暂时改名本项目 alert.conf 为 alert.conf.private-canary-paused，保留原字节。systemd依赖不能用空赋值从drop-in移除，实际start前必须核验OnFailure为空。首轮完成后恢复原文件并移除该 /run 覆盖，daemon-reload；群仍关闭。
