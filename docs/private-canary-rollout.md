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
