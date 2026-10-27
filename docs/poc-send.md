# 指定联系人单条发送测试

状态：用户已明确批准并执行一次固定文本发送，微信服务器返回 accepted；收件人手机是否实际收到仍待用户核对。前置的登录、联系人识别和私聊接收已实测通过，见 [接收记录](poc-login.md)。

## 已获确认并执行的具体动作

仅向精确唯一匹配的“测试联系人”发送一条固定文本：

> 微信接入测试：这是一条私聊发送验证消息，无需回复。

此前已约定发送前确认具体文本。用户已确认本条，且该次授权已执行一次，不再使用此授权重复发送。该模式不接受任意收件人或文本参数；默认登录和接收模式不发送消息。用户随后要求避免重复登录，旧的全新会话模式现在默认阻止运行，详见 [会话复用调整](session-lifecycle.md)。

## 实现与验收约束

- 重新登录后检查 profile，只在 created/existing 时使用既有只读联系人查找；目录不完整、重名或未找到时停止，不猜测收件人、不执行 contact-sync。
- 使用一枚本轮固定幂等键和固定 JSON，仅尝试一次 POST `/sends`；即使超时、断连、异常响应或结果未知，也不重发、不更换键再试。
- POST 返回 202 后仅轮询绑定的 send UUID，最长45秒并受剩余工作期限约束；queued/submitting 只代表处理中。只有 accepted 且 account/id/destination/clientId/serverId 校验通过才报告微信服务器接受。
- 微信服务器接受不代表收件人已读。随后由用户核对是否实际送达。
- failed/needs_review/cancelled 或结果未知均停止；不转投其他联系人，不执行媒体、read-state、ack 或同步接口。
- 控制器输出仅固定状态、错误代码及 submitted/server_confirmed 布尔值；不输出真实内部标识、令牌、上游异常或聊天正文。submitted 只表示已尝试调用提交接口，不能单独证明服务器接收。

测试继续使用相同128MiB内存、CPU20%、32tasks、32MiB私有tmpfs及固定微信域名候选地址。发送模式原生扫码操作300秒、控制器330秒、外层360秒，完成或失败后确认断开、停止原生端、销毁临时资料并核对宿主网络和业务进程基线。现有Docker、FRP和AstrBot保持原状。

发送结果不明时，先在收件人手机核对，不自动再次执行本测试。当前临时资料会销毁，不声称跨新会话保留原生幂等记录。

## 2026-10-25 实测结果

单元 `astrbot-wechat-login-2f0df2b640a0.service`：profile created、target_matched、单次提交、queued、accepted。send_result 为 ok=true/submitted=true/server_confirmed=true/code=ok；最外层退出0。未自动重发或发送第二条。

随后确认断开并清理全部临时单元、TAP和资料；host_namespace_unchanged、host_network_unchanged、business_processes_unchanged 均为 true。流量观测出站117078、入站133854字节。该次临时资料已经销毁，无法从本轮 tmpfs 恢复。

## 固定上游接口依据

- [POST 接口和202响应](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-api/src/media.rs#L275)
- [状态与服务器接受校验](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/runtime.rs#L1031)
- [发送不确定时不得重试的协议说明](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-protocol/src/client.rs#L881)
