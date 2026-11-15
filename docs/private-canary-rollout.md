# 私信灰度记录（2026-11-13）

## 已授权范围

仅现有绑定联系人；所有群入站/发送关闭。用户发起1条短文本，最多1次简短模型回复；本轮不测试图片、语音、表情或链接，不改 AstrBot/Docker/FRP/宿主网络，不重启 Native 或重新登录。

## 已完成源码与验证

privateOnly 默认false且兼容旧配置；Native、host/core和IPC三处限制目标。正式身份、持久游标锚点、恢复账本与activate保留。历史群身份种子只读核验不会授予群投递权限。开发/设计/交接已同步。

6项定向验证0.434s通过，相关84项1.863s通过，旧配置缺allGroupsEnabled覆盖privateOnly问题新增断言先失败再修复通过。静态复审发现并修复两项问题：旧字段补齐覆盖privateOnly（P1），以及部署工具写配置前未先拒绝.runtime父链符号链接（P2）。后者增加1项合成拒绝用例，先失败后通过；最终复核无剩余Critical/Important。全量1232项仍引用0452ba7，不重复宣称是本轮新跑。

## 真实只读预检

已取得用户 sudo 授权，仅通过关闭回显的标准输入使用，未保存凭据。通用root shell曾被自动审批拒绝（权限过宽且当时兼容缺陷未修复），未运行；修复后改用限定的单次只读命令获准执行。

原正式桥failed，停桥原因previous_image_send_requires_review。Native保留原进程，账号online且authenticated，剩余租期超过5天；首次探针未通过，后续明确GET在线证明通过。桥队列2860条forwarded，无queued/publishing/uncertain。53条旧桥动作结果未决（26无result、27ret1408），保留不重发；最新文本/语音accepted，图片unknown但audit_cleared=true、隔离记录仍在。selected_pair与_admit真实证明通过，ledgerRevision=3、3项Native例外已逐条覆盖。上述53项桥动作不等于53项新增Native未知提交，不能清理或当作重发许可。

## 切换状态

正式副本已由5ce68e5快进到97e6bc6，原有未跟踪私有目录保留；root工具已备份配置/unit，写入privateOnly=true、关闭全部媒体/链接/上下文并同步公开代码视图。首次操作在 start 前有效unit核验处停止：Restart=no生效，但OnFailure空赋值不能移除已有依赖，服务仍failed/MainPID0，未启动或发送测试消息。

定位依据为宿主systemd255实况及[官方unit文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit)关于依赖不能在drop-in中清空的规则。修正为校验并暂时改名精确本项目 alert.conf，保留原字节，/run覆盖仅设置Restart=no；仍在start前核验OnFailure为空。增加1项临时文件用例先失败后通过，与路径用例共2项0.007s通过。支持自身已知中断状态重入，保留第一份备份。SSE/WS就绪与用户消息结果按实际操作追加。启动失败保留privateOnly、审计和队列，不自动回退到群模式或重发。只有真实ready之后才通知用户发送授权的那条私信。


### 首次真实启动的保护停止

1cc0f68已更新到正式副本，控制器曾active，随后在worker启动前以previous_image_send_requires_review停止。原预检漏了forward-image-live-send.json（submitted、未处置）；现改用recovery_dispositions.AUDITS权威清单，保留全部审计与队列。

进一步定位到旧image_send_recovery.py的只读GET误用/sends/<jobId>，正确为与正式发送器一致的/jobs/<jobId>。增加本地HTTP回归先失败再修复；28项恢复专题0.009s通过。未更改known_failure严格条件（目标/作业绑定、阶段、零已发送字节、无服务端回执等）。下一步读取精确作业证据；未取得证明之前不再启动。


### 后续接力时的实际现场（北京时间2026-11-13 16:45之后）

本轮继续时确认正式源码为c255eed，forwarder仍failed/MainPID=0，Restart=no、OnFailure为空。Native owner及其临时Native unit已经不在；仅本项目媒体维护timer仍等待，维护service因无法pin Native失败。不能据此断言维护任务导致会话停止。保存owner状态为interrupted/done，native_stop_ok=true，final_snapshot_saved=false；不重用早前的在线结论。没有执行Native恢复、扫码、logout或重新发送。

只读读取现有约326.7MB自动快照（未发owner请求或生成新快照），精确匹配live审计幂等键及目标/源摘要：对应作业为needs_review/uploading，completedBytes=0、无nativeReceipt/服务端ID、原审计无jobId。严格proved_pre_submit=false，因此不能改称确定未发送。快照共有4笔待决，原账本覆盖3笔，唯一新增缺口为该live图片；_safe_pending检查允许将其登记unconfirmed_quarantined/operator_review，仍需本次用户明确选择，不继承旧许可。

现有快照的已接受自身身份证明存在，桥持久游标的真实锚点在该快照中一致；桥队列无queued/publishing/uncertain，绑定私聊在游标之后的未处理入站事件数为0。可以据此准备保留全部原审计、只为这一确切作业增加隔离处置，再基于保存资料走正规恢复协调器的方案。不能只改旧审计状态或直接启动旧恢复脚本，不能恢复旧清单而丢弃这笔作业。

已向用户提出具体选择：将这一笔登记为“结果未知、隔离且不重发”，并用已有保存资料恢复已停止的Native，继续私信文本灰度；或保持停桥等待人工核对。当前等待该回答，尚未写新处置账本或恢复会话；真实测试消息/模型调用仍为0。

工具补充offline-evidence模式，不要求Native owner存在，只读核对既有快照、账本、身份与锚点；不会修改私有Native行、发送审计或接收游标。主动生成新快照的检查曾被自动审批拒绝（会写owner状态请求），未执行；之后采用读取现有自动快照的更安全方式完成上述证据核验。

### 本次明确恢复授权

用户已选择“按此方案隔离并恢复原会话”：仅对上述精确live图片增加unconfirmed_quarantined/operator_review处置，不将未知结果改成确定失败，不重发。允许通过已有保存资料恢复停止的Native；不扫码、不fresh login、不动其他服务。原私信1条/最多1次简短模型回复及关闭群/媒体的范围继续有效。

本次准备路径：核对本项目维护service无进程后持media-maintenance、bridge、startup和recovery互斥，暂停维护timer；备份原审计/账本/journal/桥与清单指针，以现有快照和当前真实接收锚点发布包含全部4项处置的新不可变清单，沿用epoch与旧保护下界，不推进或改写桥记录；发布成功后归档并移走旧startup journal，由正式forwarder创建新attempt恢复。保留ticket/history及旧Native行，首轮维护timer保持暂停。操作和真实就绪结果随后追加。

限定工具`tools/prepare_private_canary_recovery.py`提供apply/restore两个互斥入口，要求正式原工作副本、受审完整提交和显式代码视图一致。新增2项真实合成SQLite/快照/账本/清单验证，先因缺少工具失败，最终1.368s通过；覆盖原审计/桥全库/旧账本/快照/ticket/history保留、完整四项准入、旧下界保持、额外待决拒绝及精确完成重入。工具中断后保留备份并拒绝自动继续。新增只读`--receive-backlog`用于Native恢复后分页核对，不移动持久游标。未重复全量或真实模型测试。

执行前独立静态审查通过，无Critical/Important。restore只到receive_ready不启动worker；真正起桥前必须要求只读积压检查complete=true、真实锚点/当前配置联系人验证通过、private_incoming=0。发布审计339文件、0发现；这一结论仍不是线上恢复或私信交付成功。

只读联系人核验复审发现一个P2：ContactReceiveClient不仅按search查询，还按模块TARGET_NAME精确过滤；现与正式Native一致先配置该名称，再调用_target，避免有效非默认联系人被误拒绝。该问题在任何恢复操作之前发现并修正。

### 已执行隔离与同次恢复（北京时间2026-11-13后续）

正式源码已快进到df34135，显式视图258文件更新3项。限定apply在线完成，结果pending=4/ledger_entries=4；旧live审计仍submitted且audit_cleared=true，原字段未改、未重发；固定私有备份为状态目录下private-canary-recovery，旧startup journal仅在新清单发布后归档移走。维护timer已暂停。

首次restore已消费一次性票据并启动Native owner。owner恢复较慢，协调器到awaiting_network/restore并按原退避进入冷却，未达到receive_ready；未重复restore。该owner原64MiB/48MiB、5%CPU预算出现memory.high和CPU节流，但无OOM，本次未更改预算。随后只读GET确认同一次Native已online/authenticated；剩余约604510秒，现有4项admission通过。只读分页扫描1343事件complete=true、原始接收锚点及当前配置联系人都通过、private_incoming=0、cursor_changed=false。

据此仅启动正式forwarder，让原journal继续同次恢复与冷却，不重置attempt、不再恢复或扫码。等待真实SSE/WS与ready；此时start命令成功只表示unit开始运行，尚非转发就绪。真实消息和模型调用仍为0。若接力不得再次运行prepare/restore或移走新journal来跳过冷却，先检查当前精确unit和最新状态。

### 握手后保护停止与精确缺陷

冷却后同一次协调器已到ready，Native/host worker曾启动；控制器在随后消费历史SSE时以media_egress_frame保护停止。停止回执cleanup_ok=true/native_session_unchanged=true；owner仍ready/connected/ok，未再次恢复Native。尚无真实私信收发或模型调用。

源码定位为Native的media.auto-download缩略图分支无条件发media_wait/prewarm，未核对imageInput开关，而控制器在全媒体关闭时按契约拒绝该帧。正确修复是在Native发送前按图片输入能力门控，关闭时照常持久checkpoint/ACK消费事件并继续文本，不能放宽控制器或启用媒体绕过。正在补一个真实子进程/本地SSE回归并做定向复核；不重跑全量。

该缺陷已作一行能力门控修复。真实Native子进程+合成SSE新增回归先复现“disabled input emitted media_wait”，再验证关闭无预热、开启保留原预热、背景/群事件checkpoint2+3正常ACK、绑定私信cursor4继续交付；与相邻文本/图片共3项1.152s通过。仅改Native发送侧能力门，broker/lease/网络均未改；现有显式角色清单已覆盖该源码，不增加模块。

### 当前结论：已部署就绪，等待用户单条私信（北京时间2026-11-13 18:14:50）

4898d97已通过限定部署工具上线，正式forwarder和原Native owner均active；Native未在本次桥更新时重启。最新完整只读核对：ready=true、SSE=true、astrbot_connected=true、native_connected=true、自身身份核验=true、private_only=true、all_groups_enabled=false，状态年龄2秒；Native online/authenticated，剩余约603521秒。群收发、所有媒体/链接/上下文均关闭，源码中的allGroupsEnabled=true由privateOnly覆盖，不能只看该原始配置判断权限。

最新清单与admission验证通过，ledgerRevision=4、四项隔离精确匹配。桥仍为2860条forwarded，无queued/publishing/uncertain；53项历史桥动作保留不重发。当前配置联系人及持久锚点GET核验通过，完整扫描1053个后续事件中private_incoming=0，未写游标；背景事件继续按原SSE/ACK顺序消费，未跳到最新。私信/群的received、forwarded、replies本轮均0，未发送真实测试消息或调用模型。

接下来请用户仅向绑定联系人发送一条短文本（例如要求只答收到），只核对该条及最多一次简短模型回复；尚不算端到端用户验收。首轮完成前保持本项目Restart=no、失败通知文件暂停和维护timer暂停，之后按原记录恢复这些精确项目策略，privateOnly保持。后续群开放、媒体或追加真实调用仍需新范围。已有自动快照恢复成功不表示停止瞬间最终快照、真实宿主重启、物理掉电或七天稳定性已验证。

### 就绪后自动快照窗口暴露的owner预算缺口

最终复查遇到Native unit FreezerState=frozen，桥仍active但SSE/ready临时false；不是新增未知发送或用户消息。精确root owner仍为64MiB/48MiB、5%CPU，已有memory.high和CPU节流证据，未OOM；其当前约327MB快照复制/校验期间冻结过长。控制器已有1024MiB/768MiB/100%，但session_runtime.start_owner的外层owner启动参数未沿用该足量有界预算。

本次修复只把root owner的三个预算对齐为1024MiB/768MiB/100%，保留swap0、tasks16、Nice19、期限、文件上限及全部隔离；不修改Native或worker预算，不去掉快照/校验，不强制thaw，不延长超时。先补精确启动参数定向验证，再审查并对现有精确owner应用同值runtime属性，无需重启Native/桥。当前等待该修复后的新鲜健康证明，旧ready记录不能替代窗口后的确认。

owner预算源码修复已完成，仅三值变化；新增精确恢复启动参数用例先因旧值失败，再与CapacityContract共5项0.008s通过，确认其他全部安全属性保留。未为此重跑全量、重启或主动生成快照。等待复核后仅对现有owner应用runtime同值并观察既有自动快照/连接恢复。

### 最终接力状态（北京时间2026-11-13 18:28:15）

owner预算修复af9b608已同步正式源码及258文件显式视图（更新2项），并对原精确owner在线应用MemoryMax=1073741824、MemoryHigh=805306368、CPUQuotaPerSecUSec=1s；owner与forwarder PID均保持，Native自然thaw，没有手动thaw、重启Native或重启桥。窗口后GET确认Native在线认证、绑定联系人/接收锚点通过，完整扫描7个后续事件无私信积压。此后只读状态重新达到新鲜ready=true，SSE/WS/Native/身份均true，private_only=true、all_groups_enabled=false、requires_review=false；最新私信和群received/forwarded/replies均0。

当前可以通知用户发送获准的唯一短文本，最多一次简短模型回复。旧图片隔离不重发、四项账本及全部历史结果保留；本轮尚未做真实消息交付验收。仍保留首轮Restart=no、失败通知暂停及维护timer暂停，完成该条用户验证后恢复精确项目策略，群媒体保持关闭。此次只新增有效定向覆盖（准备2项、Native相邻3项、owner预算相关5项），未重复全量。新预算生效和窗口后连接恢复不等于已证明全部后续快照时长、七天运行或物理重启。

### 用户已发送后的实际核对（北京时间2026-11-14）

用户通知“发了”后，首次新鲜GET为authenticated=false、connection=disconnected；旧桥仍有SSE/WS连接，但私信计数0，不能用这些连接推断微信已登录。使用保存登录resume一次成功，未扫码、initialize、logout或替换profile，原owner及Native PID保持。操作时桥仍在运行，随后进入requires_review并保护停止，cleanup_ok/native_session_unchanged均true。

静态追踪确认一种与现场时序一致的竞争：resume持command.lock至完成，安全点捕获非阻塞取同锁或观察busy会失败，经checkpoint_failed/recovery_checkpoint折叠为requires_review；现有通用错误码不能证明这就是唯一现场根因。后续外部登录恢复应先暂停桥，确认命令完成、owner非busy且无未消费请求，再启动，不能同时与桥自动捕获争用命令锁。本轮未修改生产异常分类或放宽恢复保护。

只读复核selected/current快照、四项admission/ledgerRevision=4、完整发送审计、接受身份种子、真实接收锚点、coverage和无rebase barrier通过；桥全库保留2860条forwarded及981笔旧动作（53笔历史未决）。恢复后原私信异步出现：游标后仅一条绑定联系人的live短文本，事件ID、sender、目标和类型经GET精确核对；未改正文、origin或游标，未补发消息。完整扫描281事件，唯一原私信仍未处理，桥全库摘要前后相同。

在上述门控下，原requires_review journal原样归档到固定私有目录`private-canary-warm-review-20261114`，其证明文件绑定原journal与桥全库摘要；保留清单、账本、所有审计、票据历史及接收边界。该目录还保留本次root专用guarded-serve入口。单次操作工具只在宿主临时目录，未加入公共发布或通用恢复流程。

为防启动窗口发生额外Native恢复，本次实际serve进程使用原owner PID/ticks/receipt绑定的Warm适配器：owner不匹配即拒绝，prepare/launch禁用，online仅GET。精确既有`/run/systemd/system/astrbot-wechat-forwarder.service.d/zzzz-private-canary.conf`暂加ExecStart覆盖到上述root私有guarded-serve.py，保留Restart=no与失败通知暂停；基础unit/生产源码未改。原有普通startup会在owner消失时冷恢复，不能只在启动前检查一次或以启动命令成功宣称没有Native操作。临时入口静态复核无Critical/Important，并用本地合成对象核对原owner通过、缺失/变化owner和prepare/launch拒绝；没有重跑全量。

实际新鲜ready已恢复，Native/SSE/WS/身份均通过，原owner和Native未重启。原始私信已由原游标按序处理：私信received=1/forwarded=1，桥inbox为2861条forwarded；群收发仍0，群和所有媒体继续关闭。此时回复动作已预留但尚无新增接受结果，不能把旧accepted审计当作本次回执；下一步只等这一原始请求结果，不重放或补调模型。

### 本次最终结果：用户收到回复，桥随后保护停机

用户已明确确认“看到了回复”。本次新forward-send审计（创建/更新晚于本次门控证明，目标摘要与原绑定私聊一致）为accepted，回复正文9字节；仅GET该审计绑定的send和消息投影，`text_send_recovery.readback`通过，当前Native未变化。未输出或另存正文，没有补发或重复触发模型。至此仅证明这一条私信和简短回复实际交付，不等于持续运行通过。

正式bridge随后以`onebot_action_requires_review`保护停机，unit failed/exit1，cleanup_ok=true/native_session_unchanged=true；原owner及Native保持同一PID，账号GET仍online/authenticated。最终inbox为2861条forwarded、无queued/publishing/uncertain；本轮private received=1/forwarded=1，但replies=0。本次桥动作变为ret1408：旧27笔加本次1笔为28，连同26笔旧NULL结果共54笔未决；Native已接受的真实证据与OneBot动作结果发生不一致。不能按replies=0断言微信未发送，也不能删动作或重发。

等待期间控制器状态曾180秒不更新，主线程wchan=balance_dirty_pages，累计读字符约47GB、写入约11GB；同时owner非busy、Native未冻结。源码中host IPC文本等待43秒，core外层45秒，超时保守落ret1408，host action_rejected会触发上述停机；控制器单线程捕获期间不能处理IPC。这些证据支持慢磁盘/同步快照处理与回执期限冲突的方向，但尚无精确现场调用栈或本地慢I/O回归，不能把唯一根因或修复写成已完成。`record_native_outcome(accepted_verified)`本身直接返回，不能将这次延迟误说成它主动为成功发送生成快照。

本轮未修改生产源码或延长超时、跳过快照、关闭保护；没有再次启动桥，也未更改这笔ret1408。保存登录恢复和实际warm启动结果已独立静态复核，临时guard还做了最小本地合成拒绝验证；未重跑全量。下一步先在本地复现并修正慢捕获/IPC回执协调，保留接收锚点和恢复安全点，再为本次已接受但桥结果未知的单笔动作设计GET证明后的精确幂等核对；不能让用户再发一条来覆盖问题。

当前仍保留Restart=no、失败通知暂停、维护timer暂停及本次临时guarded ExecStart；失败尚未解决，因此没有恢复自动重启或维护。接手时先看真实unit/配置/审计，journal虽为ready也不能替代已停止桥的健康证明。群/媒体配置仍关闭，原四项Native处置未变，全部历史结果及私有归档保留。后续额外真实发送/模型调用和群开放需要新的明确范围。


## 2026-11-14 回执与捕获隔离修复（源码状态）

用户已批准按[修复设计](superpowers/specs/2026-11-14-receipt-recovery-isolation-design.md)与[计划](superpowers/plans/2026-11-14-receipt-recovery-isolation.md)实施。以下实现与验证记录形成于部署前，最终上线结果见本节末尾；部署前现场桥为failed，原Native在线认证，inbox仍2861 forwarded、无待交付记录，动作未决仍54。GET扫描267条后续事件，真实锚点与绑定联系人通过、private_incoming=0、未移动游标。

- `text_send_recovery`现在识别accepted审计绑定的唯一NULL/1408短文本动作；停桥、原Native PID/ticks与GET投影证明、完整备份及事务CAS通过后才修正该行。启动不再因accepted审计跳过这笔未知结果，不重发或修改历史其他动作。
- proxy保留最多32笔/300秒文本回执绑定，DeliveryService只对完整单段证明调用存储CAS。错误ID/目标仍拒绝，多段局部成功不报整体成功。没有补发WS响应或模型重试。
- 普通1400/1403/1501拒绝作为动作失败；1408文本进入发送/模型交付暂停，主控制器继续处理IPC和诊断。root审计须匹配requestId、分段key、目标摘要及sendId才能解除对应暂停。绑定随失败WS写回保留；迟到的旧失败报告不会重新锁住已核对动作。未绑定未知及其他动作的暂停不被一起清除，完整性/存储故障仍保护停止。
- 正式捕获为host动作/发布暂停 → Native暂停 → host FIFO落盘确认 → 固定Native/bridge副本 → 有界root作业证明/发布。host有在途动作时延后捕获，不暂停Native，不延长43/45秒发送等待。普通固定副本完成即恢复收发；有rebase待核对时保留暂停直到完整证明。作业300秒、1024/768MiB、100%CPU、swap0/tasks16，主循环不做大归档IO。
- `CaptureJob`只拥有一个精确unit及捕获token/worker代际，取消先关闭控制管道撤销提交权，再证明作业/cgroup停止。最终指针替换前重核父进程/Native/profile、处置账本和live保护范围/原barrier；清单提交后才推进两代最小保护下界。新刷新及容量请求跨作业保留，容量仍需两个独立周期。
- owner mailbox锁忙/busy/待消费命令有明确busy分类，尚未写入新请求时可以延后；其他快照或身份错误继续保护。新模块和测试已登记显式PUBLIC_CODE与受限runner。

已验证：回执/动作/owner锁专题81项1.640s、capture/runtime收尾49项0.816s通过；真实Native子进程合成暂停/恢复及真实捕获子进程的固定边界、慢复制撤销指针提交均通过。恢复migration/lifecycle/coordinator/manifest等142项通过；该批另有一个测试模块名写错导致整条命令失败，正确wire专题补跑6项6.470s通过，不能把失败命令写成全绿。首轮真实systemd受限108项通过，零跳过，隔离/资源/cleanup及Native/现有业务基线全部通过；收尾102项2.650s通过，最终真实systemd受限119项通过，零跳过；隔离/资源/cleanup、原Native及现有业务基线均通过。未重跑1232项全量，未发送新消息或调用模型。

参考仍为本地NapCat 72912f2：采用OneBotAction的动作异常边界和network独立生命周期、worker的消息回传/任务所有权。保留微信的root隔离、单次提交、持久CAS、真实锚点和提交顺序。`Store.pairs()`减少同次重复两代读取；更完整的归档验证复用、owner独立定时快照协调、真实七天稳定和物理重启仍未完成，不宣称所有IO延迟均已消除。

独立静态复审已完成：发现并修正“本地超时门未立即关闭”和“指针fsync后缺少最终撤销检查”两项Important；审查者独立运行两条新回归2项0.953s通过，批准当前源码及精确单笔对账/暖启动工具，无剩余Critical/Important。没有把旧重构批准作为本次批准。


### 已执行上线与最终核对（北京时间2026-11-14 23:21）

修复源码提交`46bf472`经独立静态复审批准，无剩余Critical/Important；正式源码已从8d68140快进到该提交，262文件显式代码视图已同步。本轮没有重启Native、恢复登录、发送新消息或新增模型调用。

精确限定工具在bridge/recovery/maintenance/command锁下核对原owner、私信配置、当前9字节accepted审计及唯一动作，执行原Native GET/投影证明、完整备份和单行CAS。原1408修正为ret0；其他动作逐行、inbox、游标和已有身份映射核对保持。成功动作842→843、1408为28→27、NULL仍26，历史53笔未决完整保留；没有重发，也未把历史例外当成重试许可。

随后在原root私有warm-only实际serve入口下启动正式修复代码，保留原owner/Native PID与start_ticks绑定。线上观测到捕获进入committing时主循环状态年龄仍1–2秒、Native未冻结；后续recovery=ready、capture_phase=null、捕获作业0，至少一个完整线上捕获/发布/保留周期结束。运行238秒时Native/SSE/WS/身份/ready全部true，pending_send_reviews=0，私信/群received/forwarded/replies均0，群和媒体保持关闭。此为无新消息观察，不是重复真实私信验收。

最终只读检查：forwarder和原owner均active，Native在线认证、会话不变；队列仍2861 forwarded、无其他状态；新清单与admission通过、ledgerRevision=4/四项隔离不变，未知数53。运行期status没有结束时的cleanup_ok/native_session_unchanged字段；旧inspector将缺失字段显示false，不能据此推断Native变更，实际PID/ticks另有精确核对。

最终本地证据为102项2.650s及真实systemd受限119项通过、零跳过，隔离/限额/cleanup和Native/业务基线通过；未重跑1232项全量。发布审计与人工差异核对通过。独立审查发现的两项Important都有先失败后通过的回归，审查者又独立复跑2项0.953s并批准。

**保留的灰度策略：** Restart=no、失败通知暂停、媒体维护timer暂停和原warm-only ExecStart继续保留，以维持本次“复用原Native”的范围；未恢复普通入口的冷恢复能力或开放群/媒体。下一位AI不要再次对账这笔已成功动作、重复消费恢复票据或重跑prepare/restore。日常收发与后续测试授权应分清：本轮没有新的真实消息预算。更完整的验证复用、owner独立定时快照协调、长期稳定/物理重启仍是单独后续工作。

### 2026-11-15 私聊媒体启用授权与停止顺序修复

用户明确要求开启图片收发、语音输出和链接卡片处理；仅绑定私聊，群继续关闭，不自行增加主动测试发送/模型调用。开始前新鲜只读检查为ready、Native在线认证，当前运行received/forwarded=1、成功回复动作3，inbox2862 forwarded、无待处理，历史未决仍53；这些是用户日常流量的计数，不解释为3次模型调用。

四项开关启用前预检通过：原Native/owner不变，媒体容量足够，既有registry与live摘要相等的临时副本确认auto_download_max_bytes=0，旧媒体审计accepted或已处置。首次仅停止桥时，systemd先停正在提交的捕获unit，父桥随后进入requires_review；结束cleanup_ok/native_session_unchanged均true、uncertain0，发送审计和53项历史未决未增，四个配置开关尚未写入。

manager事件与源码一致：BindsTo+After反向停止先杀子；仅Before会与父同步stop形成等待，真实合成unit已复现。最终Before+BindsTo与capture专用signal-first/stop --no-block配套，仍证明终态/PID0/原cgroup空和EOF撤销；重复close成功后幂等，其他unit停止方式及全部预算不变。定向89项通过；真实systemd合成父停止顺序已通过，无Native或消息操作。旧失败尝试不计作通过，未重跑全量。

设计与待上线证明见[私聊媒体启用设计](superpowers/specs/2026-11-15-private-media-enablement-design.md)。实际恢复/开关生效及就绪结果追加在下方；本段不表示已经开启或完成真实媒体交付验收。

### 已开启私聊媒体并恢复就绪（北京时间2026-11-15）

停止顺序修复`37ff509`已快进正式副本并同步显式代码视图。真实systemd合成三场景均通过：父stop时parent_stop→child_stop→parent_clean，子提前exit0/exit1时仍识别unexpected_job_exit并完成重复close；没有Native或消息操作。独立复审认可最终源码；工具复核发现备份目录未fsync和未核验manager实际ExecStart两项Important，均已修正。审查agent随后额度耗尽，最后ExecStart断言由本轮实际inspect-stopped预检与主AI静态复核确认，不能记为工具最终独立批准。

限定工具在原owner绑定及maintenance/bridge/startup/recovery/command互斥下，重证选中清单和四项准入、seed真实GET/投影、持久接收锚点、无rebase barrier及桥库序列化摘要保持。原配置/guard/unit覆盖/journal完整备份到root私有private-media-enable-20261115；文件、备份目录和STATE父目录均先fsync，才更新能力断言及配置、同步视图并归档这次shutdown review。保留全部发送动作、审计、账本和接收游标，没有重发、Native恢复或登录操作。manager实际ExecStart在预检、归档前、start前三次精确核验为原warm-only入口。

`imageInputEnabled/imageOutputEnabled/voiceOutputEnabled/linkInputEnabled=true`已写入且由运行状态确认；privateOnly仍true，sticker/groupContext仍false，群入站/出站均关闭。首次启动快照copying时Native短暂停顿、SSE/ready临时false，之后自然恢复为新鲜ready/SSE/WS/Native/身份均true，账号GET在线认证且原Native PID/ticks不变，媒体容量可用，无新增send review或uncertain。

本轮启用后尚无真实媒体收发，私信和群计数均0；未主动发测试内容或调用模型。inbox仍2862 forwarded、无其他状态；成功动作846、1408为27、NULL26，历史未决53保留，旧图片两份审计仍隔离，四项账本未变。启用能力不等于真实图片交付、语音播放或网页理解已重新验收；链接卡片作为文本上报，语音依赖AstrBot生成record/WAV。

Restart=no、通知暂停、媒体维护timer暂停及原warm-only生命周期限制继续保留；本次只放开四项私聊媒体能力，不开放冷恢复、群、原生表情、Native重启或额外主动发送测试。不要再运行将媒体全部关掉的旧canary部署工具，也不要重做对账/prepare/restore。新能力与正常用户流量按实际计数观察，额外由AI主动发送的验证仍需明确范围。

最后运行171秒核对：状态年龄2秒，四能力true、privateOnly true、groups false，Native/SSE/WS/identity/ready全true；capture_phase=null、捕获作业0、recovery=ready，原Native未冻结且实例相同。pending_send_reviews/uncertain/媒体请求与发送计数均0。该证据覆盖本次首轮完整在线捕获结束后的就绪，不等于真实媒体收发验收。

### 2026-11-15 meme_manager图片段兼容

用户提供12:15:40的1400/unsupported_message_segment日志。新鲜只读现场仍ready，私聊received/forwarded7、成功回复动作14，图片请求1/成功0/拒绝3，语音请求0，群0，无发送暂停或uncertain，原Native同实例。inbox2869 forwarded、无待处理，成功动作860、1400为87、1408为27、NULL26，未决仍53。最近图片拒绝诊断为segment、PNG960×960/963617字节；元数据不含原始文件名，不据此追溯特定echo素材。

实际插件使用NapCat形式的file/sub_type/summary，桥此前仅允许file。合成同形消息先复现，再对白名单显示元数据作最小兼容，所有静态格式/来源/完整链/身份/幂等及回执校验保持。62项相关测试通过，独立源码复审无Critical/Important，不重跑全量、不发真实消息或模型调用。部署需旧代码先clean stop再快进，避免运行时source digest混用；真实上线状态随后追加。见[图片元数据设计](superpowers/specs/2026-11-15-onebot-image-metadata-design.md)。

### 切换暂停与精确停止复核

旧正式d740626首次停止回执为native_changed/cleanup_ok=false，但结束Native核验及后续PID/ticks、在线认证均证明原实例未变；network/business全局基线也已变化，不能将该变化说成native_changed的唯一原因。完整selected/admission与四项处置、GET身份种子、真实游标锚点/零私信积压、发送账本与全部审计核对通过后，受审工具保存这份失败及当前基线到root私有image-compat-baseline-recheck-20261115，仅启动旧代码warm-only桥；journal未改、Native未操作，曾恢复新鲜ready。

第二次停止发生于capture_phase=committing，回执为requires_review，cleanup_ok/native_session_unchanged及namespace/network/business基线均true，收发/uncertain均0；journal留在ready/requires_review。尚未更新正式源码，不把它算成clean stop，具体触发原因待定位。后续通过受审的两阶段限定工具核对并保存精确阻塞记录，再更新显式代码视图及原样归档这份journal；全部历史53笔未决、四项处置、原会话和私聊媒体配置保留。不得盲目start或反复旧版重启来绕过复核，执行结果继续追加。

### 图片兼容修复已部署并恢复就绪

两阶段工具经独立复审批准，无剩余Critical/Important。prepare在旧d740626、所有旧worker/捕获unit及cgroup为空、原Native accepted审计PID/ticks绑定下重证完整四项准入、GET身份种子、真实锚点和零私信积压；在root私有image-compat-stopped-review-20261115持久备份精确completion/journal/config/manifest及桥库/审计/账本摘要，live journal未改。正式源码随后快进固定cb691c9；apply再次证明同一10分钟内绑定状态、同步262文件显式代码视图并保持桥库/配置/审计/账本，才原样归档这份blocked journal并通过原warm-only入口启动。

没有重启或恢复Native、重发图片或调用模型，旧native_changed失败证据仍另存。此次归档是完整证据下的精确复核，不代表两次停止异常的触发原因已修复；未放宽生产故障分类或修改旧动作结果。后续新鲜ready与捕获完成结果追加如下。

最终运行178秒时，状态年龄0秒，ready/Native/SSE/WS/自身身份均true，capture_phase=null、捕获作业0、recovery=ready；原Native未冻结且PID/ticks相同。私聊四能力true、privateOnly true、groups/sticker false，pending_send_reviews/uncertain均0。单次新启动后的收发及媒体计数0，没有主动测试或新增模型调用。

正式只读GET确认在线认证，inbox2869 forwarded、无其他状态；动作ret0=860、1400=87、1408=27、NULL=26，历史未决53未变。262文件运行视图检查changed0/removed0，发布审计347文件无发现。静态图片显示字段兼容已上线，尚未做本轮真实图片交付验收，GIF/动图及微信原生sticker仍不支持。两次停止异常留作后续独立定位；Restart=no、失败通知/维护timer暂停、原warm-only入口和群关闭继续保持。

## 2026-11-16 用户确认图片/语音正常，链接未到达

用户明确确认图片和语音功能正常。只读新鲜检查时桥已停止约40分钟，结束码worker_failed、cleanup_ok/native_session_unchanged及namespace/network/business基线均true、requires_review=false/uncertain0；之前本轮私信received/forwarded8、成功回复动作23、图片3/语音1发送成功。Native原PID/ticks保持且在线认证，journal为ready/ready/ok、cold=false/launched=false。manager记录显示host先exit1，父随后清理capture/接收worker并exit1；缺少host原始异常，不能将具体根因记作已定位或已修复。

完整selected/admission、ledgerRevision4/四项处置及接收锚点通过。桥库2877 forwarded、无其他状态；ret0=883、1400=87、1408=27、NULL=26，历史未知仍53。真实锚点后仅两条绑定私信：停桥后102秒的60字节文本及停桥后2183秒的分享卡片。卡片实际GET为type49/appMessageType5/link对象、标题和URL非空，`[Link]`正文687字节，符合既有worker准入；不是链接开关或投影缺失。只输出结构与计数，不留正文/URL，不改游标。

本次准备精确限定的原Native warm-only启动：互斥下重证所有worker退出、完整准入/GET身份/原锚点、精确两条尚未处理输入以及DB/配置/审计/账本/journal保持，备份原失败后仅启动桥。源码和链接解析逻辑不变，原有两条正常用户流量从原游标继续，不额外发测试、不要求重发，正常模型调用仍由AstrBot决定。执行结果随后追加，当前不能仅凭准备完成宣称已就绪或卡片已交给AstrBot。

### 原消息已恢复转发

限定恢复工具经独立静态复审通过，无剩余Critical/Important；补充只读needs_reconciliation=false断言，排除accepted审计引发启动自动CAS。互斥准入与精确两输入证明实际通过，原worker失败回执、journal/config及各摘要持久备份到root私有link-input-warm-recovery-20261116；live journal、桥库、审计、处置账本和配置均在启动前保持。仅reset-failed/start本项目桥，原warm-only guard仍禁止prepare/launch、online仅GET；没有Native生命周期操作、生产代码变更或额外主动测试。

运行323秒时状态年龄1秒，ready/Native/SSE/WS/自身身份均true、capture_phase=null/捕获作业0/recovery=ready，原Native未冻结且PID/ticks相同。原两条私信received/forwarded均2，群0；正常AstrBot流量已产生7笔成功回复动作，其中图片与语音输出各1次，无待发/发送复核/uncertain。这里是已有输入恢复后的正常业务处理，不能写成“本轮没有任何模型调用”，也不能将回复动作数等同模型调用数。

链接解析未改，实际卡片符合旧准入，丢在桥已停止后的未消费事件区间；此次恢复未跳游标或重放已forwarded输入。forwarded证明OneBot WebSocket交接，不等于网页全文已抓取或模型已理解。host exit1/worker_failed的原始异常仍缺证，持续稳定性未因此证明；Restart=no、失败通知/维护timer暂停、群关闭及warm-only限制继续保留。只改开发/设计/接力文档并作发布审计，不重复协议/全量测试。

捕获结束后再按恢复前私有proof中的两个事件逐条GET，核对原event摘要、真实消息绑定及规范化body；只读SQLite确认普通文本与type49卡片各自恰好一行、均forwarded、规范化源摘要完全相同且payload已移除。当前状态年龄0秒、received/forwarded各2、requires_review=false/uncertain0，Native实例不变。没有用新增消息或仅总计数代替原卡片的交接证明。

最后正式只读检查为inbox2879 forwarded、无其他状态，ret0=890/1400=87/1408=27/NULL26，历史未决仍53；桥/owner均active、Native在线认证、状态年龄2秒，四能力开启且有效群关闭。262文件运行视图changed0/removed0，文档发布审计347文件无发现。
