# 图片发送重启对账

## 问题

桥发图片分两步：先把 `state='submitted'` 的审计写进
`/var/lib/astrbot-wechat-session/forward-image-live-send.json`，再调 Native 的
`POST /sends` 建一个 job，等回执。

**如果进程在这两步之间死掉**，审计就永远停在 `submitted`。下次启动时
`image_send_preflight()` 只接受两种状态：

```python
require(audit.get('state') == 'accepted' or audit_cleared(audit),
        'previous_image_send_requires_review')
```

`submitted` 两种都不是，于是桥**每次启动都在 7 秒后退出**，systemd 重启 6 次后放弃。
2026-11-09 就是这样停了 37 小时。

更荒谬的是：那张图**一个字节都没出去**。Native 那边查得到铁证——
`completedBytes: 0`、`serverMessageId: null`、`messageIds: []`、`reason: transport_unavailable`。
运行期的 `image_send_native.py:183-197 _known_failure()` 早就用这套判据认定"确定未提交"，
但**启动门从没问过同一个问题**。

## 修法：一条 GET-only 的对账路径

新增 `scripts/image_send_recovery.py`，镜像已有的
`scripts/text_send_recovery.py`（文本侧本来就有这一档）。启动门变成三段式：

```python
require(audit.get('state') == 'accepted'
        or audit_cleared(audit)
        or image_recovery.reconcile(sys.modules[__name__], path, settings),
        'previous_image_send_requires_review')
```

`reconcile()` 严格只读，绝不创建 send、不改 job 生命周期、不清无关的 unknown：

1. `state == 'submitted'` 才继续，其它状态直接放手。
2. 审计字段全部校验：`key`/`requestId` 是 32 位 hex、`jobId` 格式合法、
   `destinationSha256`/`sourceSha256` 是 64 位 hex、`0 < bytes <= 25MiB`。
3. `workers_stopped(core)` —— 扫 systemd 确认 host worker 和 reader 全停，
   否则别的进程可能正在用同一个 Native 连接。
4. `nsenter` 进 Native 的 netns，`GET /sends/<jobId>`。
5. 判据 `known_failure()`：

   | 条件 | 要求 |
   | --- | --- |
   | `state` | `failed` 或 `needs_review` |
   | `phase` | `failed` → `preparing`/`uploading`/`readback`；`needs_review` → `preparing`/`uploading` |
   | `serverMessageId` | 空 |
   | `result.nativeReceipt` | `None` |
   | `result.messageIds` | 空 |
   | `completedBytes` | **精确等于 0** |
   | `threadId` 的 sha256 | 等于审计的 `destinationSha256` |

   `submitting`/`readback`（`needs_review` 下）**刻意不接受**——那两个阶段微信可能已经
   收下了，不能自动判负。
6. 三重 CAS 后才写：`workers_stopped()` 再查一次、`pinned.unchanged()`（netns 没变）、
   `audit_path.read_bytes() == raw`（审计没被改）。
7. 写审计：`state='rejected'`、`review='native_job_proved_before_submission'`、
   `recoveryVerification={classification, jobState, jobPhase, completedBytes, auditSha256, verifiedAt}`。
8. 证据留档到 `/var/lib/astrbot-wechat-session/image-recovery/job-<jobId 后 12 位>/`。

### 为什么是 `rejected` + `review` 而不是 disposition marker

`audit_cleared()` 认两条路：有 `recoveryDisposition` marker（官方路径），或者
legacy 的 `state=='rejected' and review=='reviewed_send_not_delivered'`
（`scripts/recovery_dispositions.py:243 _legacy_classification`）。

自动对账走后者，因为它是一次**机器可复现的判定**，不需要人工签字的 ledger entry。
marker 一旦写进 ledger 就要求 `recordSha256` 与 native 库里的行绑定，而 Native 的库
在独立 mount namespace 里，每次都要 `nsenter -m cat` 导出 230MB——启动门不该干这个。

### `cleared()` 为什么要重算摘要

一开始 `cleared()` 只检查 marker 是不是 dict，结果 4 个篡改测试全过——
把 marker 里的 `completedBytes` 从 0 改成 999 也能通过。

现在 `cleared()` 逐字段校验，并且重算 `sha256(subject(audit))`：`subject()` 剔除
`state`/`review`/`recoveryVerification`/`recoveryDisposition` 后序列化。这样 review
前后的摘要一致，但任何别的字段被改都会失配。

## 验证

```bash
python3 -m unittest tests.test_image_send_recovery -v   # 27 项
```

测试分三组：`Proof`（判据真假）、`Settlement`（终态与篡改）、`Reconcile`（只读性、
worker 存活阻断、CAS 冲突、二次启动不重读 Native）。

## 与 `_pre_submit` 的区别

`scripts/restore_admission.py:100 _pre_submit()` 是另一套更严的判据，要求
`error_code == 'media_processing_failed'`、`profileProjection == 'not_applicable'`、
`result` 里有 `chunks` 键。它服务的是 ledger admission，不是启动门。

本次事故的 job 是 `transport_unavailable` / `profileProjection: 'unknown'` / 无 chunks，
**不满足** `_pre_submit`，所以它只能走人工 review。对账路径用的是运行期那套
`_known_failure` 判据——两者覆盖不同的失败形态，不能互相替代。

## 仍然需要人工的

- job 在 `submitting` 或 `readback` 阶段挂掉：微信可能已经收下，对账不动它，
  启动门继续报 `previous_image_send_requires_review`。
- Native 查不到那个 job：`read_job()` 返回 `None`，对账返回 False。
- 审计字段畸形：直接 False，不进 Native。
