# 计划1：群聊与私聊持续文本转发

更新：2026-10-26。当前入口为`run_forwarder.py`，旧`run_private_forwarder.py`仅保留为历史试运行入口。

## 使用方式

当前白名单：私聊“测试联系人”和群聊“测试群”。

- 私聊：发送普通文本，由AstrBot决定并生成回复。
- 群聊：从微信的@列表选择机器人后发问；同时兼容`/ai 问题`。
- 普通群消息、@其他成员、纯文字的“@昵称”及@全体成员不会作为机器人提及触发。
- `/ai `是群触发标记，传给AstrBot的文本段移除这个标记，其余字符保留；`raw_message`保留完整原文。原生@消息的正文完整保留，并映射为OneBot机器人提及。

原生提及依据固定上游beta.8的`mentions.state=known`、`self=true`和`source=msgsource.atuserlist`，由上游按账号身份精确核对直接提及列表。桥不按昵称猜测。参见[上游提及解析](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/profile.rs#L22)。

模型、人格、上下文及模型额度均由已有AstrBot管理。桥不保存模型密钥，也不自行调用LLM。

## 服务与配置

已安装并启用独立服务`astrbot-wechat-forwarder.service`：

```bash
sudo systemctl status astrbot-wechat-forwarder.service
sudo systemctl start astrbot-wechat-forwarder.service
sudo systemctl stop astrbot-wechat-forwarder.service

# 结构化状态，含两端连接、队列、计数、内存与OOM次数
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_forwarder.py status

# 重启桥，保留微信会话；发现不确定发送或待审队列时会阻止
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_forwarder.py restart
```

配置位于`/home/bridgeuser/astrbot-wechat/forwarder.json`，权限0600，模板为项目内的`config.forwarder.example.json`。版本化校验只允许已定义的非密钥字段，不接受任意网络端点、脚本参数或宿主设置。

`groupWakeMode`支持`mention_or_prefix`、`mention`、`prefix`。旧配置未提供该字段时保持旧的prefix规则，避免升级时扩大触发范围。更换群聊仍必须重新核对群绑定，不能只靠同名群随意投递。修改配置后需重启桥；WebUI和在线编辑尚未进入本阶段。

常驻入口支持开机启动、异常退出后有限次数重启。正常桥重启只重建转发工作进程。微信进程不在线时，只从完整私有快照恢复；缺失、损坏、身份不匹配或存在不确定状态会报错，绝不自动初始化二维码。

本阶段保留单次最多7天的Native运行上限，桥提前结束以完成清理，已经取消此前55分钟的试运行期限。这不是无限自动续期；达到上限后需显式重新启动。会话资料继续保留，无需因此主动重新扫码。

## 完整性与恢复

- 桥通过SSE接收事件，实际消息创建时间来自Native消息记录；不是把读取时刻当作创建时刻。
- 先将消息持久化，再确认游标。所有事件仅保存恢复所需的游标与摘要，不把其他聊天正文交给AstrBot。
- 快照恢复、桥重启时，对保存游标对应事件的内容摘要进行一次GET校验。发现游标回退、过期或内容不同即停止，不跳到最新位置掩盖缺口。
- 只有Native恢复校验通过后，才激活待转发队列，避免失败的恢复提前触发模型调用。
- 有界私有队列最多32条、正文总量2MiB，按入队顺序逐条转发，不合并、不因忙碌覆盖后续消息。
- 每条文本最多32768 UTF-8字节。完整保留中文、emoji、空格和换行；超限明确失败，不截断。
- 支持连续多次文本回复动作；长回复按Unicode字符边界分段，单段上限8192字节，发送间隔至少5秒。
- 每一段都核对微信服务端accepted及Native本地投影正文逐字相同，全部成功才向OneBot返回成功。部分成功或结果未知不自动重发。
- 去重按来源消息和请求身份处理，同内容的新问题/新回复不会被全局文本去重吞掉；覆盖AstrBot重启后echo序号重置的情况。
- 群聊和私聊分别验证目的地与引用消息；私聊提问不能打开群发送权限，引用不能跨会话。

OneBot事件推送没有业务级接收确认，因此无法承诺任意断电断网下严格“恰好一次”。写入结果不明的消息保留为`uncertain`并停止自动重放，等待核对。队列满或不支持的消息段明确失败，原待发内容不会被覆盖。

SQLite位于项目外的systemd私有StateDirectory，目录0700、文件0600。仅暂存待转发/不确定正文；已推送正文删除，保留有界身份及摘要。队列没有额外应用层加密，不进入源码上传或聊天日志。身份表和提交表也有容量上限，达到上限会报错。

## 已验证结果

- 前序真实私聊连续3条完整往返，用户确认含中文/emoji、无重复。
- 本轮“测试群”连续2条真实问答完整往返，用户确认两条均收到、无重复；每段Native正文比对通过。
- 切换常驻服务前，实际连续运行47336秒（约13小时9分钟），18条入站全部转发，27次回复动作成功，其中群聊6条输入/9次回复、私聊12条输入/18次回复；不确定结果为0。回复动作数可以大于输入数。
- 切换服务后SSE和AstrBot均恢复就绪，原22条已转发记录没有重放，微信会话保持不变。
- 本地完整回归520项通过，13项POSIX测试跳过；Linux私有回环沙箱86项全部通过。覆盖长文本、实际工作进程IPC/HTTP、真实aiocqhttp互通、游标恢复、原生提及、防误唤醒及服务恢复门禁。
- 用户明确选择跳过真实模型长文回显，以节省额度。长文本完整性使用固定模拟回复和合成API验证；未把这部分模拟测试表述为真实微信长文验收。

13小时记录不代表已完成7天连续运行验收。开机恢复逻辑有合成测试覆盖；本次没有重启宿主服务器来做整机启动测试。

## 资源与现有服务保护

Native及网络组件共享128MiB父slice，CPU20%、任务32、无swap；Native旁路工作进程32MiB/任务4。OneBot工作进程64MiB/CPU10%/任务8，可信管道控制器64MiB/CPU5%/任务16，均无swap。Native工作目录为32MiB tmpfs，私有快照和SQLite容量有界。

13小时运行后和服务切换时，未出现OOM，宿主网络、命名空间及Docker/FRP相关业务进程基线不变。只新增本项目自有systemd服务；未更改Docker配置、容器、FRP配置、防火墙、路由或内核模块，未重启Docker、AstrBot容器、FRP或Codex。

防循环、普通图片输入及核心功能验收已完成，长期稳定性仍待验证。2026-10-27用户明确将缓冲合并交给AstrBot的chat_plus，不在中转层开发；保留逐条上报及有界持久队列。WebUI另行计划。见[计划2记录](plan2-loop-protection.md)。
