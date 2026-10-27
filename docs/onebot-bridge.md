# OneBot 桥核心与模拟联调

后续进展：真实受限IPC及AstrBot单轮群问答已实测通过，用户确认收到“你好！”。Windows完整测试现为477项（10项POSIX跳过），Linux桥/IPC43项全部通过。当前仍不是常驻转发服务；新目标和差距见[转发层路线](forwarding-roadmap.md)。下文模拟记录保留其验证时点的边界。

2026-10-25完成独立OneBot v11桥核心、反向WebSocket客户端和真实aiocqhttp库的模拟联调。当前未连接真实AstrBot，未调用微信发送接口，也未改变Docker、FRP或AstrBot配置。此前群内唯一获准测试消息的授权已用完。

## 实现范围

| 文件 | 职责 |
| --- | --- |
| wechat_bridge/protocol.py | 目标群与触发过滤、OneBot消息数组、API动作和发送回执验证 |
| wechat_bridge/state.py | SQLite稳定数字ID、消息去重、提交前持久化及结果缓存 |
| wechat_bridge/transport.py | 带鉴权的Universal反向WebSocket、心跳、断线重连、队列和帧大小限制 |
| wechat_bridge/simulation.py | 新建本机随机端口的aiocqhttp模拟端点，以及合成微信消息和回执 |
| scripts/probe_onebot_simulation.py | Linux独立临时隔离单元与宿主业务基线核对 |

桥只处理指定群的实时incoming文本，要求完整`/ai `前缀和非空问题。白名单为空、其它群、历史、机器人自己的消息和非文本消息均不转发。同群同一时间只处理一个待回复问题；忙碌时的新触发不排队，过期问题不阻塞后续触发。

转发为OneBot数组格式，以合成的机器人at消息段触发AstrBot。这表示桥已经识别了/ai命令，不宣称微信提供了可靠的原生@标记。原始微信群、成员、消息标识保留在私有状态中；发给OneBot的身份为持久化数字ID。成员展示名暂为“微信成员+数字ID”，不冒充真实微信昵称。

支持get_login_info、get_status、get_version_info、get_group_list、get_group_info、get_group_member_info、get_stranger_info、当前待回复消息的get_msg，以及send_group_msg和群类型send_msg。成员查询只返回机器人或已观察到的成员，不声称提供完整成员目录。can_send_image和can_send_record返回false；私聊、群管理、完整成员列表及其它未实现动作返回失败。

首版发送仅支持一条UTF-8不超过8192字节的文本回复；数组只支持text及匹配当前问题的reply段。图片、语音、视频、at等其它段整条拒绝，不只发送部分内容。普通字符串中的CQ控制语法在没有auto_escape=true时拒绝。后续真实联调需要核对AstrBot的回复链是否符合这个文本子集。

回复始终绑定原始群ID，不按群名再次选择目标。只有当前有效触发才允许一次自动回复；每群默认至少间隔5秒。入站消息先去重再投递，断线不重放旧问题。相同连接、相同echo重复动作返回已保存结果，不重新提交；echo相同但内容改变则拒绝。

发送前持久化提交记录和固定native幂等键，再调用后端一次。成功必须有accepted回执，并校验账号、目标群、UUID及有效client/server ID。queued、needs_review、异常、取消和超时均不能报告成功，结果不明不重发。SQLite只保存身份、摘要和回执状态，不保存问题或回复正文，限制身份8192项、提交4096项及数据库页数，达到上限后拒绝扩张。

## 实际验证结果

- Windows完整测试集462项通过，10项POSIX测试按平台跳过。
- Linux新增桥测试28项全部通过，无跳过。
- 实际aiocqhttp 1.4.4 + Hypercorn 0.18.0端点完成Universal鉴权、群事件、机器人at解析所需查询、文本回发、数字消息回执、错误token拒绝和WebSocket断线重连。
- 验证其它群、普通聊天、历史和自发消息不进入模拟模型；重复消息不重复转发，未知发送结果返回失败、不重发，身份在状态重开后保持一致。
- 本地一次创建事件循环的卡住已定位为受限Windows环境阻止回环套接字；允许本机模拟网络后正常完成。未据此更改桥的隔离策略或云端网络。

Linux模拟由独立DynamicUser运行，PrivateNetwork内仅lo，根目录及代码只读、零capabilities、NoNewPrivileges、家目录隐藏、Docker socket及会话资料屏蔽。8项实际隔离检查通过；限制为128MiB内存、96MiB high、无swap、CPU10%、16tasks、75秒最大运行时，临时写入空间为/tmp 16MiB和/var/tmp 4MiB。

退出码0；临时单元移除，原微信进程身份、宿主命名空间/网络及Docker/FRP进程基线全部通过。没有新增或调整Docker容器、网络或端口映射，没有重启FRP，也没有重新扫码。

## 复现

在本项目独立虚拟环境中安装requirements-test-lock.txt后运行：

```bash
python -B -m wechat_bridge.simulation
python -B -m unittest discover -s tests
```

模拟只绑定新分配的127.0.0.1端口，不接收生产配置参数或真实微信凭据。Windows若执行环境禁止回环网络，需允许本次本机模拟网络；不要因此改变云端服务。

Linux隔离入口为scripts/probe_onebot_simulation.py；它依赖已核验服务器的项目verification布局、独立onebot-env及本项目既有基线检查脚本。没有安装全局软件：服务器缺少ensurepip，因此使用--without-pip创建项目虚拟环境，再由系统已有pip向该环境安装固定wheel，未安装apt包或改动系统Python。

## 当前边界与下一步

本次后端是合成的SimulatedWechat，已经验证OneBot侧协议与传输，但真实原生API和宿主OneBot连接之间的受限IPC适配还没有实现。当前没有生产启动入口，也没有开机启动或自动回复服务。模拟不证明真实模型、插件或AstrBot运行实例已通过联调。

下一步先补齐该IPC适配，继续复用已有Native会话及128MiB父slice，不放宽微信网络允许范围。之后再列出现有AstrBot新增专用aiocqhttp适配器和限群自动回复的具体变更，取得授权后连接；保留现有weixin_oc平台，不重启或重建Docker，不改FRP。模型、人格及成员上下文隔离由AstrBot处理，真实联调时另行验收，不能由数字ID映射替代。

## 协议依据

- [OneBot v11反向WebSocket](https://github.com/botuniverse/onebot-11/blob/master/communication/ws-reverse.md)
- [OneBot v11公开API](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)
- [AstrBot适配器v4.29.1](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)：消息数组、self_id路由及at查询链；实际运行实例仍须独立验证。
- [固定websockets 15.0.1客户端](https://github.com/python-websockets/websockets/blob/15.0.1/src/websockets/asyncio/client.py)
