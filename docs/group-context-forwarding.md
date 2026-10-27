# 群聊上下文上报

更新：2026-10-26。白名单群普通文本上报已部署，并通过真实群聊上下文问答验收。用户选择保留现有AstrBot插件策略；普通消息不补造@，由AstrBot决定模型调用。

## 已部署行为

- 范围仍为“测试群”和原私聊白名单；没有扩大到其他群或所有好友。
- `groupContextEnabled=true`时，白名单群的新普通文本作为普通OneBot群消息上报，不伪造`at`段，不按关键词判断消息价值。保留完整正文、原始消息时间、稳定成员ID、群ID及消息ID。
- 原生@机器人继续映射为OneBot `at`；`/ai `继续兼容唤醒，保留`raw_message`，消息正文段沿用原来的前缀转换。
- 普通文本可以被AstrBot及插件纳入上下文。是否调用模型、概率回复、AI决策、频率分析和注入多少历史，均由其现有设置决定；上报条数不等于模型调用次数。
- 用户明确选择保留现有策略：此次未修改AstrBot配置、插件代码或模型提供商，未要求关闭自动触发。只读检查发现群聊增强初始概率2%、回复后概率80%且持续120秒，频率AI分析开启；不能宣称普通消息保证零模型调用。
- 普通消息不覆盖已有唤醒问答的发送去重范围；维持同群接收后10分钟内的有界回复权限，使AstrBot按原策略选择的自动回复能够回到原群。桥本身不产生AI回复。
- 群图片仍按同一成员90秒内的唤醒提问关联，或引用图片后唤醒。普通闲聊不消耗待关联图片、不触发额外下载；不新增表情包、语音、视频能力。
- 保留自身消息过滤、持久去重、实时来源及白名单校验、32条/2MiB队列、既有资源限制、不确定提交不自动重试。历史上已被过滤的普通文本不会回补到AstrBot。

## 配置与回退

新配置项为布尔值`groupContextEnabled`，旧配置缺失时默认为`false`，升级代码本身不会自动扩大上报范围。

```bash
# 仅修改本项目非敏感配置；随后仅重启本项目桥
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_forwarder.py context enable
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_forwarder.py restart
# 如需恢复此前只上报唤醒消息的策略
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_forwarder.py context disable
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_forwarder.py restart
```

状态中增加`group_context_enabled`与`group_traffic`。`context_received/forwarded`表示普通文本，`wakeup_received/forwarded`表示@或前缀唤醒；这些都是桥流量分类，不是模型调用分类。

## 验证与上线

- Windows完整回归581项通过，20项POSIX测试跳过；Linux独立私有回环沙箱147项全部通过，无跳过。模拟使用合成消息和固定回复，不调用真实模型。
- 覆盖真实Native工作进程的HTTP/SSE/IPC、普通文本完整性与真实@、原aiocqhttp框架接收普通事件及唤醒回复、多成员/私聊交错、队列上限、跨重启去重、自己消息防回流、普通聊天不消耗待关联图片。
- 部署前核对19份代码SHA-256；代码备份在服务器本项目私有`context-backup-20261026`目录。先确认无排队、发布中或不确定消息，再停止本项目旧桥、原子替换文件、开启配置并启动。
- 新桥已ready，SSE与AstrBot均连接，`groupContextEnabled=true`，图片开关和混合唤醒模式保持；不确定0，没有扫码。旧32条已转发元数据保留，未重放旧问答。
- 模拟与上线后均核对微信原进程、宿主命名空间、宿主网络与Docker/FRP业务进程基线一致；没有重启或改动现有Docker、AstrBot容器、FRP。
- 长期全量文本运行仍受既有磁盘、身份表、队列及单次7天租期保护；当前结果不代表无限期归档或已完成7天验收。

## 真实验收结果

用户在“测试群”实际先发送普通文本“松果83。AWCTX-c7d29”，随后原生@机器人询问刚才普通消息中的测试口令。用户已确认第一条没有发送更长的指示前缀；AstrBot记录的正文与实际输入一致。

只读核对发现：普通消息在AstrBot平台历史中为单独plain段，没有at；问题包含at段。模型会话中的前一条user记录含该普通文本，后一条user记录是提问，assistant回答“AWCTX-c7d29”，来自此前普通消息。用户确认群内完整收到该回复、没有重复，无需追加模型测试。

运行快照：本轮新桥接收6条、转发6条，其中普通群文本5条、唤醒1条；完成回复动作4次、不确定0、无待转发或发布中消息，队列仅保留38条已转发元数据。该快照包含用户随后正常群聊，不能把4次回复动作解释为本轮问题被重复回答，也不能据此推算模型调用次数。既有自回复防护、图片关联与资源上限继续保持。
