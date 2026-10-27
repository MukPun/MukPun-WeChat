# 避免重复扫码：会话生命周期调整

## 已确认的原因和当前状态

旧PoC每次创建随机逻辑账号和空data-dir，登录请求固定strategy=qr、allowQr=true、forceQr=true；测试完成后确认disconnect、停止原生进程并销毁32MiB私有tmpfs。下一阶段没有可恢复资料，所以再次要求扫码。这是测试流程设计导致，不能据此认为微信要求每次重登。

已完成的一次私聊接收和一次获准发送均按旧规则清理。最后发送单元2f0df2b640a0返回服务器accepted且已销毁资料；不能假装当前存在可恢复会话。那一条发送授权已使用，不重复发送。

## 立即生效的防误触

旧的login、login-diagnostic、qr-only、receive-contact、send-contact裸入口默认返回fresh_login_blocked，在任何预检、DNS、目录创建或原生进程启动前停止。底层orchestrator直接调用也默认拒绝。明确的--allow-fresh-login仅用于必要的全新会话实验，不代表用户授权，不替代先检查可恢复会话。

用户已要求避免重复登录。后续联调不再通过此开关反复制造新会话，不自动生成二维码，不把网络错误、超时、进程重启或API读取失败当作重新扫码的理由。

## 已实现的保存和复用机制

1. 使用独立、持续运行的受限原生端；联系人、群和AstrBot桥复用同一个在线连接，不在每完成一步后disconnect或停止原生端。
2. 将整个SDK data-dir保存在专用私有状态目录中，稳定逻辑账号和绝对路径；不放项目/Git或上传资料，不打印任何key/token。保留registry、control、managed profile及其密钥关联，不能只留聊天数据库或仅凭trustRetained布尔值声称可恢复。
3. 服务重启优先从原资料恢复；使用禁止二维码的POST /v1/accounts/{id}/connect路径（内部auto、allowQr=false、forceQr=false）。活动连接正常时不再次调用connect/login。无二维码恢复可能仍需要手机确认；这不等于重新扫码。恢复失败时保留资料、区分网络/存储/认证原因并停止，不静默退回强制二维码登录。
4. 在确定无可用保存信任且不能恢复资料后，才向用户说明是否确需一次新的初始化扫码。先完成保存/恢复和重启检查，再安排这一次，避免继续消耗新登录。
5. 维持独立资源边界、Docker/FRP保护和宿主网络/内核不变。常驻前必须落实状态目录的权限、容量和日志边界，检查只作用于新服务；不因持久化而开放Docker socket、家目录或其它服务资料。

实现入口为 `scripts/session_runtime.py`，由独立控制服务持有受限原生进程，各次命令复用同一进程。试运行期限已从30分钟延长为1小时，原有内存、CPU、进程数上限不变；不设开机启动或自动重启，到期先停止原生端、保存资料再清理本次单元。没有发送命令。

原生端仍只写32MiB私有tmpfs，不获得任何宿主持久目录写权限。可信控制端短暂冻结本次客户端单元，完整复制data-dir，再在finally中解冻。持久化目录是项目外的 `/var/lib/astrbot-wechat-session/checkpoints/private`，root专用0700目录、0600文件。采用无压缩完整快照，current/previous/.next每份最多36MiB，总槽位上限108MiB；定期每5分钟、命令完成后及停止时保存。突然断电时最多丢失上次快照后的资料，不能把文件快照称为微信免扫码恢复已实测成功。

恢复时保留原绝对路径和逻辑账号wechat-primary。还原包先以root:000放入只读挂载区，核验临时客户端身份后才交给该UID以0400读取；不公开SDK资料。原生进程启动前只读检查outbox和媒体发送任务，包括WAL；有queued、running、submitting、needs_review或未知状态则停止并保留原快照，防止恢复后自动重发。

初始化只能使用明确的 `initialize --allow-fresh-login`，并且要求资料内零账号；已有账号必须使用resume。恢复错误不会触发初始化。在线且已认证时，resume只检查状态，不再connect；需要重连时只调用禁止二维码的connect入口。初始化成功后不会主动disconnect。

## 2026-10-25 服务器离线留存验证

已在服务器独立临时单元中完成两次空账号启动，未调用登录或生成二维码，原生端仅有回环网络：

- 第一次创建5个SDK文件，共45185字节；冻结并保存的ZIP为45697字节。
- 停止后从完整快照还原，第二次启动仍为0账号。
- API token、storage key、cursor key逐字节哈希比较均相同；只输出比较布尔值，不输出密钥或哈希。
- 第二次再次验证冻结、保存、解冻和最终停止保存。两轮退出码均成功，单元、临时挂载和staging目录全部清理。
- 两轮的宿主命名空间、网络及Docker/FRP进程基线全部通过。IPv6 RA路由寿命自然递减由既有比较规则归一化，不是路由变更。
- 验证资料位于独立selftests子目录，验证后删除，未读取或上传真实微信会话。

此项证明资料可保留并供原生端再次读取；单靠空账号测试不能声称真实微信免扫码恢复成功。此前获准发送的一条消息不会再次发送。真实会话实测结果见下一节。

同日也完成独立owner控制服务的离线启动、状态查询、从上轮保存资料还原、停止保存和完全退出实测，owner_smoke=true，退出码0。实际cgroup限制核实为memory.max=64MiB、memory.high=48MiB、swap=0、cpu.max=5000/100000、pids.max=16；原生端及其网络助手仍受原128MiB/20%CPU/32任务父slice限制。现有Docker/FRP及宿主网络检查通过。在该轮离线验证结束时，owner单元退出，保存的仅为空账号资料。

最终Linux离线单元测试347项全部通过，含POSIX权限/快照测试；Windows同一测试集通过，10项POSIX测试按平台跳过。关闭过程中会等待新owner完全退出后才向调用方返回成功。若收到退出信号时正在等待接收或恢复连接，只向经过核验且pidfd固定的本次控制器发SIGTERM，让其停止原生子进程并保留最终保存窗口；不按进程名或进程组发信号。

控制接口：`start`仅离线启动；`start-online`使用已验证的用户态隔离网络；`status`只查询缓存状态；`resume`复用或免二维码重连；`receive-contact`复用在线连接；`stop`保存后退出。扫码PNG仅暂存root私有/run目录，完成或停止后删除，不进入systemd日志或状态JSON。测试前不要运行initialize。

普通控制命令串行执行，receive-contact最长等待10分钟；忙碌时其它命令（包括邮箱stop）返回busy，不能被当作已停止。控制服务本身到期或收到退出信号时仍执行上述最终保存。重启不会重放上轮邮箱命令；运行失败与清理成功分开记录，不能以清理成功掩盖快照失败。

## 2026-10-25 真实登录和免扫码恢复验证

用户同意推进下一步后，先核实在线运行端从保存资料还原且账号数为0。只执行一次必要的initialize，用户扫码并在手机确认后，登录返回succeeded，实时状态为accounts=1、connected=true，初始化CLI退出码0。成功状态在完整快照提交之后才发布，连接继续保留。

检查真实快照只输出权限、完整性布尔值和计数：

- 21个文件，ZIP大小1095966字节；root持有0600普通文件、单链接，祖先专用目录均为root持有0700。
- registry、api-token、storage-key、cursor-key、control.sqlite及非空managed profile均存在；不输出文件清单、微信ID、密钥、token或哈希。
- 已使用的二维码从root私有控制目录删除。真实会话资料始终保留在项目外的私有状态目录，未上传或下载。

随后只做一次受控停止/启动以验证恢复：旧原生端停止、最终保存、精确清理本次单元；Docker/FRP进程、宿主命名空间和网络基线全部通过。新运行端从同一完整资料还原，启动前通过含WAL的未完成发送检查，accounts仍为1。

没有立即调用resume，而是通过显式status邮箱命令获取原生端新状态，等待其自动恢复。最终live_recovery_result为ok=true、method=automatic、restored=true、accounts=1、connected=true、new_qr=false、messages_sent=0，验证进程退出码0。没有生成第二张二维码，也没有调用额外connect。此结果证明本次正常停止/启动可用保存资料自动恢复，不保证微信信任永不过期或所有故障都可无交互恢复。

验证后保留同一个运行端用于后续群识别和接收联调，不为下一阶段新建空账号。初始30分钟期限在长时间联调中到期后，使用原资料恢复，没有调用initialize。后续期限延长为1小时；到期保存再退出，不自动扫码或重试登录。群聊收发及AstrBot桥接尚未完成。

## 群目录检查

用户指定测试群“测试群”，并确认扫码账号已在群内、已收到新群消息。显式status邮箱命令已验证accounts=1、connected=true；精准群名搜索仍返回target_not_found。

随后按用户要求去掉全部搜索条件，GET `/v1/accounts/wechat-primary/groups?limit=20`，完整读取本地目录。实测group_catalog_summary为count=0、named_count=0、complete=true，检查进程退出码0。该调用没有中文搜索参数，不能据此归因于中文关键词编码。这里的完整仅指本地目录分页完成，不代表微信服务器上的全部群已枚举。

固定beta.8源码表明groups目录依赖本地contact.db.chat_room；只有明确群联系人元数据才建立目录行。仅收到AddMsg会建立最小联系人/会话/消息，但不会自动补齐群名和chat_room。因此下一步需要由目标群的新随机标记确认群会话，再针对该群补齐元数据，无需新的二维码。[同步投影说明](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/sync_projection.rs#L444)。

群检查由独立短时只读程序复用当前Native私有网络和API，token只走匿名管道，不写文件、参数或日志。额外程序为32MiB/24MiB high/0 swap/4任务，并共享Native原128MiB、20%CPU、32任务父slice；实际隔离10项检查通过。只清理检查程序，Native进程未变化，宿主命名空间、网络及Docker/FRP基线均通过。首次网卡枚举被AF_NETLINK限制拦住后，改用/proc/net/dev，不增加网络权限。

2026-10-25真实群标记绑定已通过：精确匹配用户安排另一位群成员发送的新随机文本，candidate_group_events=1、event_count=3、text_checked=1、marker_matched=true。群会话标识仅保存到项目外root私有目录，不输出到聊天或公开日志。group_binding_saved、group_result和group_probe_result均为ok，检查进程退出码0。清理后原生端身份未变化，宿主命名空间、网络和Docker/FRP进程基线通过。

此结果证明目标群内文本已经收到、对应会话已经绑定；尚未从群元数据核对准确群名，不能把它写成群收发闭环完成。群发送尚未执行，之前服务器accepted的消息属于私聊。

定向群资料刷新已在Linux实测通过。执行前发现限时owner服务已经正常退出，核验上轮清理成功后从完整保存资料启动，accounts=1自动恢复connected=true；没有新的二维码，也没有额外connect或initialize。

仅对已绑定群提交一次刷新，状态为submitted→queued→succeeded，name_matches和projection_complete均为true。核对返回群标识、准确群名“测试群”并提交SDK快照后，保存root私有group-verification.json。随后不带搜索条件重新读取目录，得到count=1、named_count=1、complete=true，唯一目录项为“测试群”。这证明补齐群元数据后能正常显示中文群名；不能把本地目录唯一项解释成账号只加入了一个微信群。

刷新和目录列举分别退出0，两个检查程序均清理，期间原生端身份不变、宿主命名空间/网络及Docker/FRP进程基线均通过。该阶段Linux完整合成测试418项通过；随后含群发送保护的434项Linux测试全部通过。

用户明确允许向“测试群”发送固定测试文本一条后，群发送已实测得到服务器accepted。发送前实时确认connected=true，restored=false；复用同一个原生端，没有新二维码、额外connect或重启。发送只提交一次，已保存完整SDK快照及私有accepted记录；原生端身份、宿主网络和Docker/FRP基线检查通过。用户随后确认“已在群内收到”，目标群文本收发闭环通过。该次授权已使用，不重发；AstrBot桥接仍未执行。详见[群验证记录](poc-group.md)。

## 固定版本源码核查

需要整体保留registry.sqlite、storage-key、cursor-key、api-token以及accounts下的control.sqlite、profile-keys和managed profile；SQLite需保留一致性目录及存在的WAL/journal。storage-key丢失后不能重建现存密文的解密能力，移动绝对路径也可能破坏registry中的引用。资料不得进入源码、日志或上传附件。

首选恢复入口见[API connect](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-api/src/actions.rs#L2)与[Core禁止QR的connect](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/actions.rs#L126)。持久化与加密见[Vault](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-store/src/vault.rs#L1)。原生端重启的[workers自动恢复](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/runtime.rs#L1322)优先heartbeat/renew而不生成二维码。

DNS、超时、5xx、解密或身份配置错误都不能直接解释为必须扫码；needs_login/no_usable_saved_trust也应先检查保存资料与备份。保留outbox后还需审查重启恢复，不重放此前已获服务器接受的测试消息。
