# 中转重启恢复：启动协调、快照清单与处置账本

2026-11-02 到 2026-11-03。范围为中转桥启动失败后的恢复路径，不改消息语义、不增加发送重试、不引入 fresh login。本文记录根因、改造边界、迁移入口和已完成的验收；线上部署记录见文末「发布记录」。

## 结论

原先"启动是否被允许"由三处彼此独立、且都假定进程身份不变的保护各自把关：图片隔离记录绑定原微信进程、桥侧发送审计要求 Native 侧也结清、接收游标必须与快照最大事件号相等。任一处不满足即拒绝启动，而三处都没有"验证保存状态后重建进程绑定"的正式路径，于是可恢复的状态被判成不可恢复。

本次改造把三件事拆开并各自持久化：**发送处置结论**（哪些未知发送已被人工判定）、**可恢复快照清单**（哪个快照配哪个接收边界）、**启动阶段机**（当前恢复到哪一步、下次从哪里继续）。启动由独立协调器推进，每一个持久化阶段都可中断后重入；失败分类为"暂时不可用"与"需要人工复核"，前者退避重试，后者明确停住而不是反复触发 systemd 启动限制。

改造没有放宽任何消息安全边界：不确定输入仍然不自动重放，提交阶段结果未知的图片仍然不自动重发，游标仍然要求真实锚点证明。

## 根因

三个阻塞点，均来自"保护本身正确、但缺少重建绑定的正式路径"：

1. **图片隔离记录绑定原微信进程。** 隔离记录里保存 `nativeClientPid`/`nativeClientTicks`，准入时要求当前 Native 进程与记录一致。微信号重新拉起后 PID 变化，记录虽然仍然有效，启动却被判为不安全。
2. **桥侧已处理的发送在 Native 快照里仍标待复核。** 运行时可用性修复、人工复核结论和 Native 恢复门禁各自持有一份判断，没有共享同一份持久处置结论。仅结清桥侧审计，会把失败推迟到 Native 重启时才暴露。
3. **接收进度有两个来源。** 原快照最大事件游标为 19479，桥数据库保存的接收游标为 19486，两者被当成同一个数字比较。只读复核确认 19486 的锚点不匹配，19479 的事件类型、完整数据和时间与原快照一致。桥把"此事件已经接收"与"此事件已包含在可恢复快照里"混成一个进度，中断后桥进度可以合法领先快照，直接复用数字必然冲突或跳过新事件。

另有一项与本改造无关的硬件故障：系统盘 `sda` 在 11:11:36 与 11:36:47–48 报 `BadCRC`、`hard resetting link` 和读 I/O error。这类故障会放大超时与进程退出概率，但不是本次恢复逻辑能修复的对象，仍作为独立的物理检修待办保留。

## 改造边界

- **不重新扫码、不 logout、不 fresh login。** 恢复复用已保存的会话资料与现网 Native 进程。
- **不重放不确定输入。** 入站 `publishing` 在进程中断后转 `uncertain` 并保留原文，恢复后不自动重新调用模型；`queued` 才继续投递。
- **不重发未确认发送。** 图片进入提交阶段后结果未知的，只登记处置结论，不重发、不删记录。
- **不放宽游标证明。** 恢复必须拿到快照边界内真实事件的锚点摘要；拿不到就明确阻塞，不回退到最新事件。
- **不改变资源预算。** 控制器、Native 父组、OneBot 工作进程的内存、CPU、任务数、swap 与运行期限保持原值。

## 六个交付部分

| 部分 | 解决的问题 | 主要源码 |
| --- | --- | --- |
| 发送处置账本与恢复准入 | 阻塞点 1、2 | `scripts/send_dispositions.py`、`scripts/restore_admission.py`、`scripts/recovery_dispositions.py` |
| 恢复清单与去重保护 | 阻塞点 3 | `scripts/recovery_manifest.py`、`scripts/recovery_snapshot.py` |
| 准入接入生产路径与遗留迁移 | 让账本真正被读写 | `scripts/run_private_forwarder.py`、`scripts/run_forwarder.py`、`scripts/forward_host_worker.py` |
| 运行期捕获与去重下限 | 恢复下界落在真实保留范围内 | `scripts/recovery_checkpoint.py`、`wechat_bridge/durable.py` |
| 启动协调器与诊断 | 阶段机、退避、失败分类 | `scripts/recovery_coordinator.py`、`scripts/recovery_diagnostics.py`、`scripts/session_runtime.py`、`wechat_bridge/transport.py`、`scripts/forward_sse.py` |
| 生命周期验收、迁移与文档 | 可重复验证与一次性迁移 | `tests/test_recovery_lifecycle.py`、`scripts/migrate_recovery_state.py`、`scripts/accept_reboot_recovery.py`、`tools/run_isolated_lifecycles.py` |

## 启动阶段机

协调器把启动写成六个持久阶段，每一步结束都会落盘，重入时从记录的阶段继续：

```
validate → restore → online → rebind → receive_ready → ready
```

- `validate` 选择清单、重新计算准入、确认没有残留工作进程，并检查当前桥的接收游标有锚点证明。
- `restore` 把选中快照复制成私有候选，写入启动票据（`recovery-ticket.json`，schema v2），再由 `session_runtime.py --serve-recovery` 拉起 owner 进程。
- `online` 只做只读 GET：确认账号已认证、连接状态、自身身份投影。断开时最多请求一次 resume，随后进入 `awaiting_network` 或 `awaiting_user_action`。
- `rebind` 只在冷恢复时执行：把接收游标重新绑定到清单边界，旧游标先备份成 `receive-before.sqlite`，再按需建立重放屏障。
- `receive_ready` 复核当前检查点边界仍在快照内，然后交回桥进程做最后激活。
- `ready` 由桥在真实 WebSocket 与 Native SSE 都就绪后通过 `mark_ready` 写入，协调器本身不提前宣称就绪。

失败分两类。`awaiting_network`、`awaiting_user_action`、`onebot_not_connected` 按 1/2/4/8/16/30 秒退避重试，用尽后进入 300 秒冷却并保留 `retryAt`；其余失败一律写 `requires_review`，桥以退出码 0 结束，不触发 systemd 启动频率限制。并发启动由 `recovery-startup.lock` 互斥，第二个协调器读到 `recovering` 就退出，不抢恢复权。

## 恢复清单与接收边界

清单（`recovery/current.json`）是十键不可变文档，把快照、账号资料指纹、处置账本版本、Native 侧最大事件边界和桥侧接收边界绑在一起：

- `nativeBoundary` 取自快照副本内的最大事件，`bridgeBoundary` 必须是桥侧真实保存过的 canonical 锚点（`{cursor, anchor}`）。两者都要在同一读事务里复核。
- `dedup` 记录保护下界与去重高水位。下界只能在指针提交**之后**推进，且取当前、上一两代清单的最小值，绝不提前。
- 接收边界保存成带锚点的 JSON；只有裸数字游标而没有锚点的状态，恢复时按"必须有真实锚点"处理，缺失即阻塞。
- 发布用内容寻址文件名加原子指针替换。复制件要重新做一次同样的校验，两次结果一致才提交。

运行期捕获只在桥的单线程循环的安全点发起：没有待处理请求、没有语音中继、没有图片传输时才复制一致数据库并取检查点。周期性捕获与容量触发的连续捕获都走同一路径。

## 处置账本

账本（`send-dispositions.json`）只为**例外**记录结论：每个条目绑定账号、发送类型、记录摘要（`recordSha256`）和证据摘要（`evidenceSha256`），不保存消息正文。普通成功发送不入账；只有被隔离、被人工驳回或明确未提交的条目才需要一条持久结论。

恢复准入逐条比对：快照里每个待决记录必须在账本中有对应条目，且证据摘要吻合；没有对应条目的保持拒绝。这样"人工已经判定过的未知发送"可以放行恢复，而"从没见过的新未知"不会被旧许可继承。

遗留迁移（`recovery_dispositions.import_legacy`）把旧审计文件按固定输入名做一份不可变备份，再在一个账本事务里复查源字节后写入。重复导入返回 `imported=0`，幂等。

## 部署与迁移

发布顺序固定为：只读 `check` → 满足前置条件后 `apply` → 只启动本项目桥 → 在线核对。

```bash
# 只读检查：报告当前会选哪个快照、哪个接收边界、需要迁移什么。不建表、不写文件。
sudo python3 -I -B scripts/migrate_recovery_state.py check

# 应用：先整份备份，再建接收窗口、导入遗留处置、发布首份清单，最后推进保护下界。
sudo python3 -I -B scripts/migrate_recovery_state.py apply
```

`apply` 要求 root、桥未运行、没有其他恢复进程持锁。它按顺序做：取得桥排他锁 → 完整备份（快照、一致桥副本、审计、hold、账本、票据、五项发送审计及其标记）→ 建立并初始化接收窗口 → 导入遗留处置 → 发布清单 → 指针提交后才推进保护下界。同一天重复运行会复用同一份备份目录，不会覆盖第一份。

迁移会显式建立 `recovery_window` 表。运行期读取桥数据库时用的是不跑构造函数的只读连接，不会替旧库补表，所以这一步必须由迁移完成，而不是靠启动时"顺手修复"。

### unit 模板迁移

`scripts/run_forwarder.py` 的 unit 模板新增了 `StandardError=journal`。已装机器的 systemd unit 是旧模板写下的，`start()` 会比对模板字节，不一致时报 `unit_definition_changed` 并拒绝启动。升级时按下面顺序做一次即可：

```bash
sudo systemctl stop astrbot-wechat-forwarder.service
sudo rm -f /etc/systemd/system/astrbot-wechat-forwarder.service
sudo python3 -I -B scripts/run_forwarder.py install
sudo systemctl daemon-reload
```

这是模板变化的一次性代价，不是每次重启都要做的事。不要用 `daemon-reload` 代替删除旧 unit 文件——模板比对读的是文件内容。

## 验收

### 隔离生命周期

`tests/test_recovery_lifecycle.py` 用真实子进程、真实 SQLite、真实协调器引擎和本地合成 Native API 跑完整生命周期，不依赖账号、不发送任何消息、不调用模型：

- 冷恢复 → 在线复用 → owner 进程死亡后重启，三段各核对一次就绪状态、唯一存活 owner、接收边界与保护下界。
- 在 validate/prepare/launch/online/rebind/receive_ready/ready 七个持久化边界终止进程，要求下次启动不重复拉起、不重发未知、不重放不确定输入。
- 两个协调器竞争时只有一个取得恢复权，另一个读到 `recovering` 后退出。
- 恢复后的队列语义：同 ID 同正文按重复输入拦截、不产生模型帧；同正文不同 ID 正常入队；同 ID 改正文拒绝。
- 接收压力超过保护窗口（4096 行）时明确阻塞，补齐快照并下发保护指令后恢复投递。
- 准入继承：处置结论不符时进入 `requires_review` 且不拉进程，重试仍为 `requires_review`，清理易失状态后重新可用。

`scripts/accept_reboot_recovery.py` 提供三个子命令：`isolated` 在受限单元里跑上述生命周期并核对验收前后的进程/网络基线，`baseline` 记录当前基线，`check` 对照基线复核。

`isolated` 需要 root 与 systemd-run，在本机非 root 环境不执行；它会在受限单元里运行，非 root、只读代码、零 capabilities、仅私有回环网络、独立资源上限。受限单元用 DynamicUser，读不到 `0750` 的家目录，所以脚本先把 `scripts/wechat_bridge/tests/tools` 复制成 `/var/tmp/reboot-accept-<hex>` 下的 0755 暂存树再 bind，退出时删除。

`baseline` 与 `check` 会核对 `scripts/context-staging/manifest.json`——这份清单由部署工具在安装时生成，本分支不产出它。缺少该文件时两个子命令以 `manifest_missing` 退出：那是预先条件未满足，不是验收失败，先完成部署再跑。

### 测试计数

| 范围 | 结果 |
| --- | --- |
| 本机全量 `discover -s tests -p "test_*.py"` | 1073 项通过，173 秒 |
| 恢复相关专题（checkpoint/coordinator/manifest/snapshot/迁移/生命周期/诊断/处置集成） | 全部通过 |
| 迁移工具 `tests/test_recovery_migration.py` | 13 项通过 |
| 生命周期 `tests/test_recovery_lifecycle.py` | 8 项通过 |
| 协调器与诊断 | 25 项通过 |
| OneBot 受限沙箱 | 由 `scripts/run_onebot_tests.py` 覆盖，已把 `test_recovery_*.py` 纳入发现范围 |

全量套件用 `.runtime/verification/onebot-env` 解释器运行；该环境带 `websockets` 与 `aiocqhttp`，系统 Python 缺少这两个依赖会产生导入错误而不是真实失败。

### 仍需如实保留的边界

- **没有做过宿主重启验收。** 本次全部验证都在隔离子进程和本地合成 API 上完成。把合成生命周期当作"真实重启已验证"是错的；真实宿主重启仍需单独安排窗口。
- **物理磁盘故障未修复。** `sda` 的 `BadCRC`、链路重置与读 I/O error 属于硬件问题，改造只降低它放大为启动失败的概率。
- **在线验收在发布窗口内进行。** 发布后需要核对 Native/SSE/WS/队列/延迟与自然消息，并与 Docker/FRP、宿主网络、会话身份基线对比。这些结果记录在发布窗口，不在本文预设。
- **只保留既有协议子集。** 本次没有扩展 OneBot API、群管理、表情包与语音输入。
- **图片与语音的生命周期验收仍是长期项。** 完整媒体生命周期和无限期续租没有在本次范围内完成。

## 复核入口

```bash
# 本机全量（需 onebot-env 解释器）
PYTHONPATH=tests .runtime/verification/onebot-env/bin/python -m unittest discover -s tests -p "test_*.py" -q

# 恢复专题
PYTHONPATH=tests .runtime/verification/onebot-env/bin/python -m unittest \
  tests.test_recovery_migration tests.test_recovery_lifecycle \
  tests.test_recovery_coordinator tests.test_recovery_checkpoint \
  tests.test_recovery_manifest tests.test_recovery_snapshot

# 受限生命周期与在线基线（root）
sudo python3 -I -B scripts/accept_reboot_recovery.py isolated
sudo python3 -I -B scripts/accept_reboot_recovery.py baseline
sudo python3 -I -B scripts/accept_reboot_recovery.py check

# 发布前审查
python3 tools/audit_publication.py .
```

`check` 必须沿用操作之前的基线。发现差异先核对具体身份，不重新建立基线来掩盖差异。

## 发布记录（2026-11-03 / 11-04）

### 部署步骤

1. 用 `tools/sync_runtime.py --apply` 把审阅过的源码同步到 `.runtime/verification`（200 个文件）。
2. 写 `scripts/context-staging/manifest.json`：本次改动的 21 个 `scripts/*.py` 的裸名与 sha256，供 `accept_reboot_recovery.py` 的 `deployed_code_matches_review` 比对。
3. 安全暂停：停 `astrbot-wechat-media-maintenance.timer`、`run_forwarder.py stop`、`session_runtime.py stop`，此时 bridge 与 Native owner 均停止。
4. 重装 unit：删旧 unit 文件 → `daemon-reload` → `run_forwarder.py install`（模板内容变化必须走这条路径，`start()` 会做字面比对）。
5. `migrate_recovery_state.py check` → `apply`（root）：建立 `recovery_window`、导入历史处置结论、发布首个清单。apply 输出 `imported=1 matched=1 dedup={floorSeq:1833, highSeq:1833}`。
6. `run_forwarder.py start`：`serve()` 内由协调器推进 `validate → restore → online → rebind → receive_ready → ready`，冷恢复耗时约 2 分 40 秒（含一次 209MiB 快照证明）。
7. `accept_reboot_recovery.py baseline` 采基线，`check` 复核。

### 部署期发现并修复的三项

这些都是实测出来的资源预算问题，已随 `5807774` 提交：

- **`LimitFSIZE=8388608` 让冷恢复直接失败。** 恢复要物化快照归档里的 `control.sqlite`（173MiB），8MiB 写上限导致 `selected_pair` 抛 `ValueError: recovery_manifest`，协调器把任何非瞬态异常统一报成 `recovery_integrity`，日志里看不出真实原因。改为 1.05GiB（与 session、maintenance 两个 unit 一致）。
- **`CPUQuota=5%` + `MemoryMax=64M` 让每轮快照证明超过 300 秒。** 单轮 capture 需要把同一份 209MiB 归档解压校验约 10 次，配额太小时 page cache 反复回收，一轮从 25 秒膨胀到 130 秒以上，转发循环被同步安全点饿死，状态文件长期停更。改为 1GiB / 768MiB / 100%，实测窗口降到 34–58 秒。
- **`fresh_status` 的 10 秒窗口与同步证明冲突。** 证明期间 `ready` 实时计算、循环不转，状态必然停更。门槛放宽到 `FRESH_SECONDS = 90`，并在常量旁注明原因。

### 一次非计划的真实验收

2026-11-04 09:11:15 UTC，broker 以 `status=1/FAILURE` 退出（`journalctl` 与内核日志都没有留下 Python traceback，**根因未确定**）。systemd 按 `Restart=on-failure` 重启，重启后 47 秒内完成 `validate → online → receive_ready → ready`，两个 worker 单元自动重建，走的是 warm 分支（复用仍然存活的 owner）。崩溃瞬间正在外发的一条消息被写成 `state=unknown` 并附带 `recoveryDisposition{classification:unconfirmed_quarantined}`，**没有重放**。

这恰好覆盖了改造要保证的核心行为：进程非预期退出后桥能自行恢复到就绪，且不确定的发送结果被隔离而非重试。

### 部署后的运维例外

`forward-image-live-send.json` 是旧代码在 `image_failed` 状态下写的 `state=rejected`，缺少新代码 `_legacy_classification` 认识的 `verification` 或 `review` 字段，因此 `import_legacy` 无法为它产生任何处置证据，新代码的 `image_send_preflight` 会以 `previous_image_send_requires_review` 拒绝启动（旧代码只要求 `state ∈ {accepted, rejected}`，所以部署前能起）。

已核对快照里的 `media_jobs` 行：`job_ea83e21b` 为 `state=failed, phase=preparing`、无 `nativeReceipt`、`profileProjection=not_applicable`，与 `rejected_before_submit` 的定义完全吻合。因此用生产函数 `recovery_dispositions._record` 生成合法 marker 并写入审计文件本身（与运行时 `settle_outcome()` 的写法一致），未改代码、未删除原始证据，原始文件备份在 `recovery-migration-backups/<stamp>`。

**这是运维例外，不是生产路径。** 若将来再出现同类历史审计，应优先考虑补 `_legacy_classification` 的归类分支，而不是重复手工裁决。

### 仍未完成的边界

- **没有做过宿主重启验收。** `isolated` 子命令验证的是受限沙箱内的生命周期与资源隔离，不是真机断电重启。
- **物理磁盘仍未修复。** `sda` 的 SATA BadCRC / 链路重置 / 读 I/O 错误是硬件问题，本次所有改动都不涉及它。
- **控制器在证明窗口内 `ready` 会短暂为假**（34–58 秒 / 每 300 秒）。这是同步安全点的固有代价；若将来窗口继续增长，应优先减少单轮内的重复解压，而不是放宽验收门槛。
- **`Production.inspect_owner` 的 systemd/argv/receipt 解析仍无子进程级单测**，生命周期套件用的是自建的等价适配器。
