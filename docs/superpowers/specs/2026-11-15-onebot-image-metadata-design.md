# OneBot 图片显示元数据兼容

用户于2026-11-15提供meme_manager发送图片返回1400/unsupported_message_segment的日志。排查开始时桥新鲜ready，图片输出开关true、无新发送暂停或uncertain。最近一次固定字段诊断为segment、PNG、960×960、963617字节；该元数据不包含随机文件名，不能据此追溯指定echo的准确素材。

## 已确认的原因

现用meme_manager的`_send_meme_image → _prepare_qq_sticker_chain`把图片包装为通用`_OneBotSticker`，经AstrBot的`BaseMessageComponent.toDict()`透传`file/sub_type=1/summary`。该路径区别于普通Image被适配器转换为仅file的路径。桥的`image_output.from_segment`此前要求data仅有file，因此静态图也会在上传前被拒绝。本地合成同形消息已复现1400。

参考本地固定NapCatQQ 72912f2的`packages/napcat-onebot/types/message.ts`，其图片段明确声明可选summary/sub_type；在OneBot图片解码边界兼容这些显示提示，不将QQ特有提示映射成微信原生表情能力。

## 最小修复与边界

- data仍必须有file，额外仅允许sub_type/summary。sub_type严格为整数0或1（bool、浮点、字符串或其他值拒绝），summary为最多256个UTF-8字节的字符串。
- 两字段不改变或重编码源字节，不输出到Native审计、消息正文或日志。继续只接受base64来源，走同一静态PNG/JPEG/WebP、8MiB、尺寸与完整性校验；GIF、动图、未知字段、路径及URL仍拒绝。
- 完整链、引用归属、目标/权限校验仍在动作预留前完成。原请求参与幂等摘要，echo和单次提交/回执语义不变；不吞掉非法消息段，不截取部分内容发送。
- 不修改AstrBot、插件、SDK或配置，不开放微信原生sticker或群，不重试已失败动作。

## 验证和切换

新增合成用例先复现from_segment拒绝及OneBot ret1400，再验证相同字节、echo和重复请求仅发送一次；并覆盖非法元数据、GIF及路径/URL拒绝。相关62项通过（宿主I/O抖动，104.578秒），不重跑全量，不调用模型或真实发送。

独立静态复审确认源码无Critical/Important。部署必须先在旧正式HEAD上核对闲置并停止桥、保存root私有clean completion及原Native绑定证明，再快进受审代码。不能先更新运行中正式源码：捕获子作业会核host role digest，新源码配旧hostDigest可能误触完整性保护。停止后在互斥下备份、同步显式代码视图，核对桥库、配置、发送审计和处置账本不变，仅经原warm-only入口启动；不修改journal、不恢复登录、不重启Native。

真实上线与媒体交付边界见[灰度记录](../../private-canary-rollout.md)。配置与会话仍按[媒体启用设计](2026-11-15-private-media-enablement-design.md)执行。普通1400不会因本次兼容而升级成停桥，也不代表底层已提交。

## 切换期间的停止复核

旧正式源码停止后先得到native_changed，结束时Native核验却仍未变，另有长期运行期间network/business全局基线变化；这些基线失败不产生native_changed，不能混为已证实的根因。原始失败回执经完整只读准入、真实锚点和零积压核对后保存到root私有备份；受审工具绑定accepted审计的原Native PID/ticks，仅旧代码warm-only启动，未改journal或Native。旧桥曾重新达到新鲜ready。

随后在committing捕获期间停止旧桥得到requires_review，cleanup_ok/native_session_unchanged以及namespace/network/business基线均true，收发和uncertain均0，journal为ready/requires_review。不能把此回执伪称clean stop；确切触发原因尚未证明，亦未修改生产故障分类。后续限定复核须证明所有旧worker/捕获unit与cgroup为空、原Native/owner未变、完整清单与四项准入有效、真实接收锚点和无积压、无replay barrier、发送审计已接受或精确处置且历史未知数53未增。

限定工具采用两阶段：旧HEAD下持久备份原始completion/journal/config/manifest及全部绑定摘要，live journal保持；源码快进后再次证明相同状态并同步显式代码视图，再将唯一已复核的blocked journal原样归档。实际启动仍由原warm-only入口独立验证准入与ready。任一证明失败不归档、不放行，不重发或改账本；最终执行结果以灰度末段为准。

两阶段工具最终独立复审通过并执行：cb691c9已正式部署，262文件视图一致，桥库/配置/审计/账本保持，未操作Native或发消息。运行178秒时新鲜ready、SSE/WS/Native/身份全部通过，捕获作业0、phase=null/recovery=ready，原Native实例不变，无新发送暂停或uncertain。此为部署及捕获结束后的健康证明，真实图片交付和两次停止异常原因仍未验证。
