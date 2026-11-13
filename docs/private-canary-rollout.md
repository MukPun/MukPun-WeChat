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
