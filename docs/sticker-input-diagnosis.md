# 私聊表情包接入诊断

更新：2026-10-27。用户明确将表情包处理暂缓，不作为当前转发核心的验收阻塞项。当前仍不支持表情包识图；此前完成的是失败环节定位和只读诊断，尚未查明上游HTTP 500的具体内部根因。

## 当前范围决定

本阶段不再开展表情包取图测试、下载适配或SDK修改，维持现有type47跳过策略，不新增占位回复或模型调用。诊断代码与记录保留供以后参考，未来重新纳入范围时再安排实现与验收；不因上游更新而自动升级或重启现有接入端。

用户参考的上游[issue #238](https://github.com/thisnick/agent-wechat/issues/238)主要追踪发送现有/自定义表情包及表情包商店、收藏管理。它与本次入站媒体查询500的失败范围不同，不能据此假定该入站错误已被确认或已有修复承诺；这不影响用户本次暂缓决定。

## 已确认的两处缺口

1. 线上桥目前接收文本、普通图片及带图引用。微信表情包是`type=47`，仍在Native读取层以`unsupported_incoming`跳过，没有构造OneBot图片消息交给AstrBot。
2. 用户用既有私聊测试联系人发送新表情包后，隔离GET诊断确实找到了入站type47；但现用beta.8接入端的`GET /v1/accounts/wechat-primary/messages/{id}/media`返回HTTP 500，公开错误码为`operation_failed`。没有返回可用媒体资源，标准消息投影的body为null。

第二项不能单凭公开错误码确定为某种特定图片格式、CDN域名或鉴权故障。此时尚未取得图片字节，不能把失败归因于AstrBot视觉模型，也不能把放开type47消息过滤当作已经支持表情包。

## 本轮实际操作

- 使用独立32MiB读取工作进程，复用既有Native网络命名空间和会话；仅读取指定测试私聊的近期元数据。
- 新诊断参数为`probe_loop_guard.py --sticker`。使用服务器项目下`sticker-probe-staging`中的临时代码制作只读工作包，不替换线上Native工作进程代码。
- 只输出消息类型、是否找到、媒体变体摘要、HTTP状态与经过格式校验的公开错误码。不输出消息XML、CDN签名地址、令牌或密钥。
- 未创建下载任务、未开放额外CDN出口、未调用模型、未向微信发消息、未扫码、未重启Native或AstrBot。
- 每轮结束均清理独立诊断工作进程，确认微信原进程、宿主命名空间、宿主网络及Docker/FRP业务进程基线一致。

## 上游能力与实现条件

固定版本的[媒体发现代码](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/media.rs)有type47分支，公开资源类型为sticker，只有original变体。其[下载实现](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-protocol/src/sticker.rs)限制来源主机，验证长度与MD5，并拒绝跳转。源码存在支持分支不代表本轮真实样本能够成功取图。

未来重新纳入范围时，需要先在隔离副本中定位并修复上游媒体查询失败，再实现私聊type47到受限OneBot图片输入的转换。继续保留来源绑定、大小/尺寸/时间上限、去重与防循环；不得把表情包XML、裸CDN地址或虚构描述当作图片交给模型。GIF原始字节转发与模型理解动画也是两个验收项，不能宣称支持动作或全部动画帧。

本轮未升级或替换SDK二进制。此前已通过的普通文本、群聊上下文及普通图片路径保留；需要临时识别表情内容时，可先由用户将表情另存为普通图片发送。
