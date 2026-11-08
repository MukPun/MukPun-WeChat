# 微信分享卡片（type49）输入

状态：已实现，默认关闭；需显式打开 `linkInputEnabled`。

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
2. `scripts/forward_native_worker.py` 的准入判断：
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
- **链接出站**（AstrBot 发 `share` 段让微信生成卡片）：桥的出站校验只接受
  `text` 与 `reply` 段，`share` 会得到 `unsupported_segment`。这是刻意保留的
  收敛策略，不是遗漏。

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
v1.2.0 中已存在，线上二进制为 v1.4.0。本次改动只依赖这些既有字段，
但 **v1.4.0 二进制的实际投影行为需要在真机群/私聊各发一张卡片确认**。
