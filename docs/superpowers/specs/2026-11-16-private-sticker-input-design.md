# 绑定私聊的原生表情输入启用方案

状态：用户已于2026-11-16明确批准“按方案启用私聊原生表情”；正在执行[计划](../plans/2026-11-16-private-sticker-input.md)。源码50c3077已部署；已有表情的单次原生下载取得14,327字节GIF并通过完整性核验，私聊表情开关/五能力guard已更新并仅暖启动桥，最终就绪结果见末段。此前原生type47已被明确暂缓，不能沿用普通图片成功结论宣称它已支持。

## 已确认的现场

- 桥新鲜ready，Native/SSE/WS/身份均正常，原微信实例未变；四项私聊媒体开关true，stickerInputEnabled=false、群关闭。本次不是前一轮host停机。
- 既有绑定私聊的实际消息是type47，body=null，已被当前关闭策略消费游标而不上报。不能仅改开关就承诺这条旧消息会被自动补交。
- 这条既有表情的GET媒体目录返回HTTP200及一项kind=sticker/state=pending；contentType/byteLength/preview尚未生成。历史beta.8的目录HTTP500本次没有复现，但尚未下载或取得可用图片字节，不能宣称取图已成功。
- 诊断只输出结构/计数，没有输出消息XML、来源URL、令牌、真实身份或媒体内容，也没有POST下载、模型调用或真实发送。

## 方案选择

推荐复用已实现的read_sticker及图片输入链路：Native解析/下载原生媒体，桥核对来源、大小、格式及摘要，通过既有OneBot image/file交给AstrBot。对应NapCat的参考原则是由原生适配器承担平台媒体差异，再投影公共消息；QQ face/mface编号和发送实现不适用于微信type47。

另一种方案是在桥中新建动图解码/抽帧模块，统一转换PNG或多帧。它需要新的解码依赖与CPU/内存预算，也会引入首帧选择和模型多帧语义，超出当前启用问题。此次采用既有Native预览与图片路径，不新建动画理解模块。

## 范围与行为

只为真实绑定联系人启用stickerInputEnabled，privateOnly=true继续覆盖群模式，保留现有图片收发、语音输出及链接输入配置。实际root warm-only入口的能力断言同步增加该开关，原owner/Native PID与start_ticks、拒绝prepare/launch及GET-only在线证明保持。不会开启原生表情输出、群、引用内表情、小程序表情等新消息类型。

入站链为type47 → 绑定核验及现有时间窗/容量准入 → read_sticker → Native媒体资源/下载作业 → 已核验图像 → 原图片IPC/文件交付 → OneBot image。WXGF使用Native提供的静态预览，并核对preview MIME/字节数/SHA256；PNG/JPEG/WebP/GIF按既有输入格式交付。GIF交付不等于模型理解全部动画帧；出站GIF/动图限制保持。

每张表情继续使用既有25MiB、尺寸/像素、媒体并发锁、有限请求/重试和获取总预算。不得通过放宽Native来源域名、移除媒体租约/资源限制、传裸CDN地址或XML绕过。核对取图期限与CDN临时租约的配合；已识别的取图不可用/超时走现有失败与checkpoint/ACK路径，身份/IPC/策略完整性故障仍保护处理。能力关闭时继续明确跳过type47，不发占位文本调用模型。

## 实现与验证范围

1. 补现有真实Native子进程/合成API夹具对type47开启、关闭及不可用后后续文本继续的有效覆盖；沿用现有普通图片用例，复核WXGF预览摘要与GIF原字节专题。只运行关联用例，不重跑全量。
2. 首先复用用户这张已有表情作一次受限取图检查：精确绑定原消息/资源，最多一次下载作业，遵守既有有限重试、下载/租约期限，0条主动外发、0次额外模型调用，不向AstrBot回放旧表情。保留必要审计，作业结束恢复原临时CDN策略。
3. 如需修正期限/失败分类，仅修改sticker所需的适配器或媒体实现，更新相应受限清单/runner；不改OneBot动作语义、公共存储、去重、普通图片或Native二进制。
4. 本地检查、源码及限定运维工具复核完成后，核对桥闲置、无新增未知发送，完成精确停止与原Native准入证明，备份配置/guard/状态再启用。仅重启桥，复用原Native会话，不运行旧prepare/restore工具。若停止产生review，保留证据并重新核对，不删除journal绕过。
5. 核对实际加载的warm-only ExecStart、开关、262文件显式代码视图及新鲜ready，观察现有捕获周期结束。后续新到的私聊表情按正常用户流量处理；不改游标、不自动补交已被旧策略跳过的表情。

## 文档与通过标准

同步DEVELOPMENT、sticker专题、灰度实况、设计和AI接力入口，明确本地证据、实际启用与真实模型识别各自范围。发布前审查差异和脱敏审计。

通过标准为：既有表情的原生取图/预览完整性证据成立；相关子进程用例通过；启用后原会话与私聊限制保持、桥新鲜ready；后续受支持type47可进入图片交付，不会因普通不可用重放或发占位模型请求。下载成功不代替AstrBot交接，交接成功不代替用户确认模型理解；不承诺全部表情或完整动画。

用户已批准本方案，不需要为相同范围再次确认。50c3077已将表情读取收紧至60秒，配合既有65秒hd_input租约，其余预算不变；13项针对测试3.206s及独立复审通过。原生取图已成功，未向AstrBot补交旧消息或额外调用模型，不能据此宣称真实模型识别表情或理解动画已验收。历史53笔未知、四项处置及上次host退出根因未定位的限制保持。记录见[表情诊断](../../sticker-input-diagnosis.md)及[灰度末段](../../private-canary-rollout.md)。

实施中的停止回执再次报native_changed/okfalse，但cleanup、原Native身份及宿主三项基线均true，journal仍ready/ok。没有把它改写成clean stop；受审工具只接纳这次精确completion/failure摘要，经完整准入/原owner/Native/种子/锚点/零积压及旧worker退出证明后，持久备份异常并继续同范围启用，journal未修改。下载一次、作业终态及策略恢复证明均保存在root私有目录；无来源/端口或资源限制扩展。

root warm-only guard仅替换能力断言，五项媒体开关true、groupContext false、privateOnly true；新SHA256为de71a4037cf5dcecddbd4f58f626da8c2846bf497ea853740f0344479aed94c3，Warm类与其他字节不变。实际ExecStart保持同一路径，Restart=no、OnFailure空、媒体维护暂停。新源码正式显式视图262文件，旧工具的四能力/sticker=false断言及旧guard摘要已经失效；不得重复执行本次一次性probe或旧prepare/restore工具。

最终只读核验通过：状态年龄1秒，ready/Native/SSE/WS/自身身份true，capture0/phase=null/recovery=ready；原Native/owner、私聊五能力与群关闭保持。inbox2879 forwarded、旧未决53，262文件代码视图匹配，原CDN策略恢复；新启动后收/转/回均0。真实AstrBot表情交接/识图未额外验证。
