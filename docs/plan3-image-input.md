# 计划3：图片输入转发

更新：2026-10-27。图片桥已启用，真实微信取图、无模型联调及私聊视觉模型往返通过。用户手动开启现有群聊增强插件的图片处理后，确认01:19群图问答描述正确、完整收到且没有重复，群图真实往返验收通过。长期媒体稳定性仍未完成。

## 使用方式

- 私聊白名单“测试联系人”：新图片可直接交给AstrBot；也支持引用图片并输入问题，将图片与提问作为一个OneBot事件发送。引用必须由Native解析到同一账号、同一会话，不能跨会话取图。
- 群“测试群”：同一成员先发图，再在90秒内原生@机器人或用`/ai `提问；或者直接引用图片并原生@/使用`/ai `提问。单独群图片不唤醒模型。最多记住16位成员各一条图片关联，不合并连续文本，不混用其他成员的图片。
- 模型、视觉能力与回复内容仍由AstrBot配置。桥不调用独立模型、不保存模型密钥。可用“引用图片，要求不超过20字”完成低成本验收。

## 图片与资源限制

第一版单图上限128KiB，最长边2048像素，总像素最多4194304。校验MIME、文件头尺寸、长度、SHA-256和base64往返完整性；不截断图片、不使用远程用户URL或宿主文件路径。

优先使用Native已缓存、符合限制的标准图或原图；未缓存时只申请缩略图，不自动下载大原图。图片质量随元数据传递。缩略图适合验证基本识别，不宣称具备原图细节，细小文字可能无法辨认。

Native取图最长10秒，每次只处理一图，每个工作进程最多新建32个下载任务；失败不会自动重试。数据通过既有匿名IPC和OneBot `image`段中的`base64://`传递，不新增监听端口或Docker挂载。IPC单帧仍为256KiB，持久队列仍为32条/2MiB。图片待转发超过5分钟即清除正文并记录expired，继续处理后续消息；不确定提交保留待核对，不自动重放。

桥不建立额外图片文件目录；待转发图片仅在项目外私有SQLite暂存，交付后清除。Native自身缓存由SDK管理，仍受32MiB私有存储上限约束。日志只保留状态、字节数、质量与摘要，不含图片字节或CDN凭据。

## 图片CDN的隔离出口

固定上游beta.8通过消息服务器取得CDN配置，图片下载使用独立公网IP的443端口。原slirp出口只允许启动时核验的消息服务器地址，因此真实首次下载返回`transport_unavailable`、0字节。

新增出口租约仅作用于本项目已存在的slirp cgroup：从已核验wx二进制进程实际持有的TCP套接字识别单一公网443目标，不能接受IPC提供的IP。核对UID、进程启动标识、原白名单、helper身份、SDK身份与最多32个/32规则后临时增加一个地址，任务结束撤回；等待控制设置45秒保护窗口，单次取图请求另有10秒截止，进程退出时也尝试撤回。多目标、不明进程或策略冲突均拒绝。

系统d接口的列表赋值按追加处理。真实验证中发现首次撤回校验失败，已使用显式清空加原集合恢复并确认精确一致；回归增加追加语义和撤回失败不能报告关闭的用例。该过程只调整项目自己的cgroup出口，没有加载内核模块、修改宿主防火墙/路由、重启微信或改动Docker/FRP。

## 验证记录与运行边界

- 本地完整回归565项通过，17项POSIX相关测试跳过；Linux独立私有回环沙箱131项全部通过。
- 合成Native HTTP/SSE和实际工作进程覆盖私聊图片、群图与提问、引用图片、相同会话绑定与投影回执；实际aiocqhttp框架完成图片输入及固定文本回复，无LLM调用。
- 超限、MIME/摘要篡改、禁用开关、重复消息、自身消息、过期图片与后续消息，以及临时白名单的单目标、身份变化、应用失败与撤回路径均有验证。
- 用户提供的真实图片缩略图已取得：JPEG、1272字节，内容摘要与Native元数据一致。原白名单、宿主网络、命名空间、微信会话及Docker/FRP业务进程基线核对通过；未扫码、未调用模型。
- 已启用`imageInputEnabled=true`，切换只重启本项目桥；Native会话不变，没有新的二维码。SSE与AstrBot均就绪，旧22条记录没有重放，资源上限保持不变，未出现OOM。
- 私聊真实验收：用户引用此前图片并要求不超过20字。接收1条、图片准备1次、转发1次、AstrBot回复1次、微信accepted1次，不确定0；用户确认描述正确、完整收到、没有重复。该图片来自已校验的1272字节JPEG缩略图。
- 私聊验收轮次后自身发出事件拦截1次，未形成循环；当时出口状态`image_egress_active=false`，持久队列只剩23条已转发元数据。该时点尚未完成群图验收；群图随后通过情况见下文，长期媒体稳定性仍未完成。

### 群聊实测排查

用户已在“测试群”发图并原生@提问，但未收到正确图片描述。桥记录了群图等待、图片准备、带图事件交付及微信回复回执；这些计数不能替代模型实际看图的验收。

只读核对AstrBot持久记录发现：原始平台消息包含`at + text + image`，图片字段已被AstrBot解析为本地文件引用；模型会话中则只有文本和`[Image]`占位符。没有证据表明桥按@位置截掉图片。

进一步核对已安装`astrbot_plugin_group_chat_plus`源码及配置：`enable_group_chat=true`、`enable_image_processing=false`、`image_to_text_scope=mention_only`、图片转文字提供商为空、`enable_private_chat=false`。其`ImageHandler.process_message_images`在关闭图片处理时，会过滤图文混合消息中的图片；这解释了群聊失败而私聊正常。

用户随后手动修改AstrBot配置，并确认群聊已能看到图片。只读复核当前`enable_image_processing=true`、`image_to_text_scope=at_only`，且已选择图片转文字提供商；这是用户自行选择的配置，本项目未修改或重载该插件。插件在指定图片转文字提供商时先生成图片描述，因此后续模型会话不一定保留`image_url`段，不能仅凭没有该段判断图片被丢弃。

此前复核时最新带图平台记录仍为当时的输入，尚无充分记录证明修改后完成了新的群图识别。当时将群聊状态记为“用户已修复插件配置并确认可见，正确描述及回群待确认”；该时点项目转发服务及微信会话所有者均为active/running，自动重启计数均为0。

2026-10-27收尾复核找到北京时间01:19的新群图输入，AstrBot原始入口保留`at + text + image`，随后有文字回复；桥记录图片准备/交付各1次、当前实例微信接受回复7次、不确定0。用户核对这轮现有结果并明确确认“描述正确，完整收到且没有重复”，群图真实往返验收通过，没有追加发图或模型测试。Linux隔离147项及本轮进程/网络基线核对通过；历史桥自动重启计数现为1、具体原因缺少日志，长期稳定性不标记为完成。详细范围和未完成项见[核心验收记录](core-acceptance.md)。

本阶段不支持微信type47表情包、语音/视频、向微信发送图片或完整OneBot v11所有API。2026-10-27用户明确缓冲合并由AstrBot的chat_plus负责，不在中转层开发；WebUI另行计划。

用户随后反馈AstrBot表情包管理器向微信发送图片返回`invalid_request`。已修复拒绝错误过度影响文本服务的问题：返回`unsupported_message_segment`并单独计数，确定未提交的图片请求不再停止桥；图片输出能力仍未实现，`can_send_image=false`保持。详细验证及发布记录见[图片发送拒绝处理](outgoing-image-policy.md)。

2026-10-27表情包诊断及范围调整：已找到用户新发的私聊type47，但现用接入端媒体查询返回HTTP 500 / operation_failed，没有可用图片字节，具体内部根因未确认；线上桥也尚未接入type47。用户随后明确暂缓该项，保持跳过策略，不再探测或修改SDK，不作为当前核心验收阻塞项。只读诊断未调用模型或修改线上策略，详见[表情包接入诊断](sticker-input-diagnosis.md)。不能将普通图片验收结果扩展为表情包支持。

## 操作

```bash
# 查看真实样本状态；不交给模型
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/probe_loop_guard.py --image
# 对已提供样本取图并校验；不交给模型
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/probe_loop_guard.py --image-download
# 配置开关后，仅重启项目桥，保留微信会话
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_forwarder.py images enable
sudo python3 -I -B /home/bridgeuser/astrbot-wechat/poc-stage-01/run_forwarder.py restart
```

参考固定上游：[媒体接口](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-api/src/media.rs)、[下载与缓存](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/media.rs)、[引用图片解析](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/quotes.rs)。
