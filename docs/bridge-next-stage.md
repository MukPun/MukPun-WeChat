# 群收发通过后的 AstrBot 桥接步骤

最新进度：本页所列IPC已实现，真实单轮问答已通过，用户确认另一位群成员收到“你好！”。以下保留阶段核对记录；当前产品目标、功能差距与后续顺序以[转发层路线](forwarding-roadmap.md)为准。

当前微信测试群“测试群”的真实文本收发已通过，用户确认群内收到唯一获准测试消息。该次授权已用完，后续准备不再发送测试消息。

## 新增适配器前的只读核对结果

2026-10-25检查现有AstrBot主配置，仅输出适配器类型、启用状态、端口及鉴权是否存在的布尔值，未输出模型配置、密钥或原配置内容：

- platform_count=1；唯一类型weixin_oc，enabled=true。
- 主配置没有aiocqhttp适配器。
- 宿主6199和6185均有监听。该事实只证明监听存在，不能证明OneBot的/ws端点已经启用。
- 未连接OneBot或触发机器人回复，未调用Docker管理接口、修改端口映射、容器或FRP配置。
- 宿主AstrBot源码目录的版本常量为4.24.2；此前运行实例核对为4.29。两者不能混为一谈，实施时须按实际运行实例的适配器接口验证，不升级或替换现有实例来迁就本项目。

## 用户新增适配器后的检查

同日用户在AstrBot后台完成配置后，已进行只读复测：主配置中aiocqhttp平台数为1，enable=true，端口6199匹配。对127.0.0.1:6199/ws执行Event角色短握手，无Token请求返回HTTP101，证明WebSocket端点已经启动。该连接随后关闭，未发送lifecycle、heartbeat、message事件或API动作，也未注册API角色客户端。

首次复测时ws_reverse_token为空，authentication_enforced=false、token_bridge_compatible=false。用户随后在AstrBot中保存Token后，再次只读复测通过：anonymous_http_status=401、authenticated_http_status=101、authentication_enforced=true、token_bridge_compatible=true、websocket_handshake_ok=true、ok=true，退出码0。适配器已启用且端口6199匹配，AstrBot端配置和鉴权已就绪。

Token仅从服务器私有配置读入内存，没有输出、下载或写入项目。检测只使用Event角色短握手并关闭，events_sent=0、api_actions_sent=0、configuration_changed=false；不注册API角色、不发送群消息、不修改或重启服务。当前检查不能证明真实微信IPC或AstrBot的模型回复闭环已完成。

## 已完成的 OneBot 核心模拟

已实现独立OneBot桥核心，并用本项目自己的真实aiocqhttp库端点验证消息转换和回执。Windows完整462项测试通过（10项POSIX跳过），Linux新增28项全部通过；服务器8项隔离检查及宿主网络、Docker/FRP和原微信进程基线通过。该验证不连接现有AstrBot、不接收真实聊天正文、不调用微信发送接口。详见[实现与模拟记录](onebot-bridge.md)。

首版只面向已验证的一个群，使用严格/ai前缀触发；不依赖未经验证的微信@标记。群和成员原始身份映射为持久化数字ID，重启不漂移。非白名单、机器人自己发出的、历史或未触发消息不进入AstrBot；发送失败和未知结果不能返回成功，也不自动重发。

桥复用原生端现有会话。原生API仍在私有网络内，微信域名允许范围不扩展至宿主业务；原生侧工作进程与宿主侧OneBot连接需要受限IPC，不能直接取消网络隔离来访问AstrBot。资源上限、保存资料和不自动扫码的要求继续适用。

## 待桥实现并验证后再安排的 AstrBot 变更

需要新增一个专用aiocqhttp平台，保留当前weixin_oc平台：独立平台ID，反向WebSocket端口使用已发布且经实际验证可用的6199，设置新生成的非空鉴权token；桥只连接服务器本机端点。token保存在项目外私有目录，不进源码、聊天或日志。

不改现有模型、人格和其它平台配置，不重启或重建Docker容器，不修改Docker网络、端口映射或FRP。新增平台是否能在实际运行版本中热加载，须在连接前核实；如需要重启现有实例则停止该路线，先提出替代方案。

这些变更尚未执行。下一步先补齐真实微信私有网络与宿主OneBot连接之间的受限IPC适配；当前仍使用合成微信后端。完成后将具体新增字段和限群触发行为一并列出，再按此前约定取得现有实例变更的授权。群内自动AI回复也不复用已消耗的单条测试发送授权。
