# 持续私聊文本试运行

这是历史55分钟试运行记录。当前已升级为群聊与私聊常驻入口，操作与验收以[计划1持续文本转发](plan1-text-forwarding.md)为准。

本阶段落实计划1的持续文本转发与SSE接收。范围是唯一匹配联系人“测试联系人”；群聊仍保留此前已验收的单轮入口，不会默认开放所有好友或群。

## 使用

在服务器运行以下项目命令，均不管理现有Docker或FRP：

```bash
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_private_forwarder.py start
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_private_forwarder.py status
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_private_forwarder.py stop
```

启动只复用完整保存资料，并禁止自动生成二维码。Native会话当前仍有1小时保护期限，桥最多运行3300秒，且提前于Native期限结束。停止桥保留Native会话和快照。这个期限用于初次持续联调；24小时运行及群聊持续入口需要后续验收，不能把本试运行说成正式全天候服务。

`ready=true`须同时满足SSE连接、AstrBot反向WebSocket连接，以及微信账号连接探针（`native_connected=true`）。前两者只证明本机IPC与反向WS存活：微信长连接静默断开时它们仍为true，因此状态另带`native_connected`/`native_connection`/`native_probe_code`/`native_checked_at`，由只读线程每30秒在Native网络命名空间内GET一次账号资源得出；读取失败按`unreachable`计，不按健康计。状态中`received`是本轮入站事件数，`forwarded`是成功写入WebSocket的文本事件数，`accepted_parts`是微信服务端接受的文本分段数，`completed_replies`是全部分段均accepted的OneBot回复动作数。多分段回复会使分段数大于动作数。

`forwarded`不代表AstrBot已完成处理，`accepted_parts`不代表收件人的手机已显示。真实验收必须核对手机收到的正文。

## 完整性处理

- 入站保留前后空格、换行、中文及emoji，不去除前缀或改写文本；私聊无需群聊的`/ai`触发规则。
- SSE订阅固定Native API；断线使用已确认游标恢复，不请求历史同步、不调用登录接口。只有成功持久化输入后才确认相关事件游标。
- 入站私有SQLite队列最多32条、正文总量2MiB；按入队顺序逐条推送，不因忙碌忽略后续消息。此处是待转发队列，不合并消息；2026-10-27用户已明确消息合并交给AstrBot的chat_plus，不在中转层开发。
- 单条输入/模型回复最多32768 UTF-8字节；超限明确失败，不截断。Native API自身的响应上限同样适用。
- 模型回复中的所有文本段按原顺序拼接；支持连续多次`send_private_msg`/`send_msg`动作。单次回复超过8192 UTF-8字节时按Unicode字符边界分段，段间至少5秒，全部接受才返回OneBot成功。
- 不支持的图片、语音等消息段使整个回复动作失败，不会只发送其中的文字并谎报完整成功。
- 发送前写入幂等提交记录；相同echo在重连后仍使用同一命名空间。一次发送结果未知不自动重复POST；部分成功也不重新发送已成功的段。
- 过滤非目标联系人、非incoming、非live及自发回声；以源消息ID去重，不以正文相同为由吞掉正常新消息。

## 故障语义

OneBot v11的WebSocket事件没有业务级接收确认。因此无法承诺任意断电/断网下的“严格恰好一次”。写入中断时将原正文保留为`uncertain`，停止自动重放；需要核对记录后再恢复。队列满时停止接收并显示故障，已有队列不会被覆盖或悄悄清空。

SQLite位于项目外、systemd管理的私有StateDirectory中（目录0700、文件0600）。待转发和不确定消息会暂存正文；已成功推送的正文会删除，保留有界身份/摘要元数据用于去重。它不是聊天记录日志，也不进入代码上传。当前没有对队列另加应用层加密。

Native进程重建时使用新的游标命名空间，避免把恢复后回退的SDK游标当作当前游标。已入队内容保留；两次Native快照之间尚未入队的消息不能宣称已获得本队列的持久保证。

## 隔离与验证

Native读写工作进程32MiB/4任务，在原Native私有网络和128MiB父slice内；OneBot工作进程64MiB/8任务、CPU10%；可信管道控制器64MiB/CPU5%。均无swap。没有安装系统软件、修改内核模块、防火墙、路由、Docker配置或FRP配置。

新增测试覆盖Unicode原样保留、24条突发FIFO、32条满队列、进程重启去重、发送中断保留、长回复多段、多次回复、部分成功、未知不重发、目标绑定、SSE分帧/心跳/重连游标、真实aiocqhttp私聊互通，以及实际Native工作进程的匿名管道/HTTP往返。

2026-10-26，Windows完整回归497项通过（11项POSIX跳过），Linux隔离环境63项通过。真实私聊验收：测试联系人连续发送A/B/C三条问题，接收3、转发3、微信accepted回复3、完成回复动作3、不确定0；用户确认三条全部完整显示、第一条emoji正常且没有重复。没有生成二维码。此结果验证了这一轮连续私聊文本往返，不等于已完成全天候或真实超长消息验收。
