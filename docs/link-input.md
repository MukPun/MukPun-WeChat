# 微信分享卡片（type49）输入

状态：已实现，配置默认关闭；2026-11-15起已在绑定私聊中开启。群仍关闭，实际运行状态见[灰度记录](private-canary-rollout.md)。

## 结论先行

微信分享卡片**不需要下载任何媒体**，也不需要桥侧重建内容。SDK 的 Native 投影
把 `LinkCard` 直接拼成一段文本放进消息 `body`（`wechat-core/src/links.rs` 的
`LinkCard::body()`），桥只做准入判断，然后把它当普通文本转发。

因此中转层能给出的是**文本形态的链接**：

```
[Link] 标题
描述
https://example.com/...
```

而不是 OneBot 的 `share` 消息段。

## 为什么不发 OneBot `share` 段

OneBot v11 有 `share` 段，AstrBot 的 `aiocqhttp` 适配器也会把它解析成
`Share` 组件。但 AstrBot 组装模型 prompt 时只取 `event.message_str`，
再遍历 `Image / Record / File / Video` 追加附件（`astr_main_agent.py`）。
`Share` 组件既不进 `message_str`，也不在附件白名单里——**只发 `share` 段，
模型什么都看不到**。

所以桥选择让链接走文本通道：`raw_message` 和 `message` 里都是可读文本，
模型与插件都能直接使用。代价是 `share` 段的语义信息（缩略图 URL、
子类型）不传递，这与本项目"只实现 AstrBot 联调所需子集"的范围一致。

## 消息流

1. Native 投影把 type=49 且 appmsg subtype 为 `3..=5` 或 `51` 的分享卡片解析成
   `link` 对象（title / description / url / sourceName），并把
   `LinkCard::body()` 写进 `body`。
2. `scripts/forward_native_worker.py` 加载 `wechat_bridge/adapters/wechat/native.py`；后者的准入判断：
   `linkInput` 开关打开、消息 `type == 49`、且 `link` 是对象，才认定为本能力覆盖的消息。
3. 绑定校验：`id` / `accountId` / `threadId` / `direction == incoming` /
   `senderId` 形状 / 私聊时 `senderId == peer` / `body` 是字符串。任一不符即
   `WireError('link_source_binding')`。
4. 记录按 `type == 1`（文本）组装，`body` 原样带过。
5. 下游与普通文本完全同路：`message` 事件里是 `{type: 1, body: "[Link] ..."}`。

## 覆盖与不覆盖

覆盖：

- 公众号文章、网页分享、小程序卡片等落到 subtype `3/4/5` 的分享卡片。
- 视频号分享（subtype `51`，走 `finderFeed` 取描述与昵称）。

不覆盖：

- `LinkCard` 为空（title / description / url 全空）的消息：SDK 返回 `None`，
  桥按原样继续判为 `unsupported_incoming`。
- 引用消息内嵌的卡片（subtype `57`）：那是引用能力，不在本能力范围。
- 表情包、视频、文件、拍一拍：各自独立的能力，见对应文档。
- **链接出站**（AstrBot 发 `share` 段让微信生成卡片）：原生卡片输出尚未实现，
  `share` 返回 `unsupported_message_segment`。现有文本、受限静态图和语音输出见开发文档。

## 大小上限与超限行为

`LinkCard` 对 title / description / url 分别限长（16384 / 16384 / 8192 字节），
理论上拼出来的 `body` 可以超过桥的 32 KiB 文本上限。

超限时**不抛异常**：worker 会发一条
`{"kind": "report", "stage": "unsupported_incoming", "type": 49, "reason": "oversized"}`
并照常推进游标（durable 模式发 checkpoint，非 durable 模式直接 ack）。

之所以不 `raise`：抛异常会让 worker 进程失败，而这条消息在游标没前进时会被
recovery 反复重放，形成拿不掉的毒丸消息。丢弃并留一条 report 更安全。

## 配置

```json
{ "linkInputEnabled": true }
```

- 默认 `false`——旧配置升级后不会自己开始转链接，与
  `imageInputEnabled` / `stickerInputEnabled` / `groupContextEnabled` 的收敛原则一致。
- 命令行开关：`python3 -I -B scripts/run_forwarder.py links enable|disable`。
- 与图片不同，它与 `allGroupsEnabled` 有交互：全群模式下链接按普通消息上报。

## 验证

```bash
python3 -m unittest tests.test_onebot_links -v
python3 -m unittest discover -s tests -p "test_onebot_native_forward.py"
```

端到端用例覆盖：私聊分享卡片、群内分享卡片、超过 32 KiB 的卡片被丢弃一次、
开关关闭时卡片不上报。

## SDK 版本说明

链接字段（`links.rs` 的 `LinkCard`、`profile.rs` 的 `link` 投影）在源码树的
v1.2.0 中已存在，线上二进制为 v1.4.0。2026-11-16对用户已有私聊卡片的GET核对已证明：type49、appMessageType5、link对象、标题/URL及687字节的`[Link]`正文均由实际Native正常投影，符合现有准入。群尚未开放或作对应验证；不能由私聊证据宣称全子类型/全群均已通过。

## 2026-11-16 链接未到达的现场诊断

用户已确认图片及语音正常，但分享卡片未到AstrBot。新鲜核对时桥已经worker_failed退出，结束cleanup_ok/native_session_unchanged均true、uncertain0，原Native仍在线认证。manager事件证明host先exit1，之后父控制器清理capture与接收worker；没有保留host原异常细节，停桥根因仍未证实。

这条有效卡片在停桥后2183秒才到达，另有一条60字节普通私信在停桥后102秒到达，二者都在真实持久锚点之后且未入桥队列。完整GET扫描无其他绑定私信，原游标未动；桥库2877条forwarded、无queued/publishing/uncertain，历史53笔未知与四项Native隔离未增。此次不需要修改卡片解析器或开启新的消息类型。

恢复只复用原Native的warm-only桥入口，先核对完整准入、原owner/Native、全部旧worker退出、账号/seed/锚点和精确两条原输入，持久保留worker失败回执。桥库、配置、账本、审计及live journal均保持，不调用Native恢复或主动发送接口；原消息由正常服务按原游标继续处理。实际启动与交付结果见[灰度末段](private-canary-rollout.md)，不能让用户重复发链接替代原消息核对。

恢复后该原卡片与前一条文本均已逐条核验：私有proof绑定原事件GET摘要，规范化正文/身份/目标摘要与持久inbox一致，各自仅一行且forwarded、payload移除。捕获结束后桥新鲜ready、原Native不变，无发送复核/uncertain。此证据证明链接文本已完成OneBot WS交接，不承诺AstrBot已经抓取网页全文或模型理解内容；host退出根因仍未证实。
