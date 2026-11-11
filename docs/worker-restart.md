# Worker 死亡后的就地重启

## 结论

两个 worker（`forward_native_worker.py`、`forward_host_worker.py`）中任意一个退出，控制器不再
直接退出，而是就地重拉两个 worker 并重走一次握手。只有**十秒内第三次死亡**才判定为持久缺陷，
由控制器退出交给 systemd 与告警单元处理。

## 为什么阈值不是 systemd 的那套

单元文件里是 `StartLimitBurst=3` / `StartLimitIntervalSec=600`，即十分钟内三次失败就永久放弃。
这个阈值是按"启动即失败"的配置错误设计的，不适合长跑进程的偶发死亡：本系统在 2026-10-25 至
11-10 之间平均每天退出 13.5 次，十分钟里撞上三次属于常态。2026-11-09 那次三十七小时宕机正是
撞上这个阈值后 systemd 放弃重启、而当时还没有告警通道的结果。

就地重启改用 NapCatQQ 的判据：十秒窗口内三次。偶发死亡十秒内复现不了，能复现的是持久缺陷。

## 重拉什么、不重复什么

两个 worker 共享一次**线性**握手：`isolated` → `native_ready` → `host_ready` → `native_armed`
（`activate`）。因此换掉任意一半都必须重谈整条链，不能只补一半。

不重复的是控制器自身的**准入**：固定 Native 会话（pin）、回放 checkpoint、运行
`image_send_preflight`。那部分以数百 MB 计，也是控制器唯一值得保留的启动成本。

## 重拉时必须补的三件事

1. **`inbox` 里停在 `publishing` 的行要退役。** 它们属于已经不存在的管道。`Spool.__init__`
   只在进程首次构造时做这件事（见 `durable.py` 的模块说明），控制器不退出时这段代码永远不会
   再跑，所以重启路径显式调用 `Spool.quarantine_publishing()`。不退役就重放，等于让同一条入站
   消息可能触发第二次模型调用。
2. **输出请求要结算。** 控制器手里可能有一个已发出、未收到回执的图片或语音请求。`abandon_request()`
   镜像最终清理块的逻辑：审计未 cleared 就标 `unknown` 并按 `unconfirmed_quarantined` 落处置账本。
3. **每个 worker 单独记账。** `native` 与 `host` 各有自己的窗口与预算，一个 worker 的死亡不消耗
   另一个的额度。

## worker 侧不需要改动

worker 本来就无状态：它每次启动都等控制器发 `start` 帧才推进，所有业务状态在主进程 SQLite 里。
所以重拉只是换一个干净的哑进程。

## 新单元名

每次 `launch()` 都重新铸造 unit 名（`secrets.token_hex(6)`）。上一代的 `--collect` 还在拆解时，
新名字不会撞车。已停止的单元从 `started` 列表里移除，避免最终清理块重复 stop 一个已经不存在的
单元。

## 观测

`status` 里新增 `worker_restarts`：

```json
"worker_restarts": {
  "native": {"name": "native", "crashes": 0, "restarts": 0, "window": 10.0, "limit": 3},
  "host":   {"name": "host",   "crashes": 0, "restarts": 0, "window": 10.0, "limit": 3}
}
```

`crashes` 只统计**当前十秒窗口内**的死亡，会随时间自然归零；`restarts` 是累计值。就地重启成功时
`journal` 里会有一条 `{"kind":"report","stage":"worker", ...}`，`counts.attempts` 是该 worker 的
累计重启次数。

## 边界

- 重拉不重启 Native 会话（不重新扫码、不重新 pin），所以 `native_changed` 这类 pin 失效仍然走整体退出。
- 崩溃前的 `inbox` 行**不会**被证明"已送达"。它们变成 `uncertain`，与进程整体退出时的处置一致。
- 十秒内第三次死亡 → `raise Failure`，由 systemd 走 `Restart=on-failure`；超限则由
  `astrbot-wechat-forwarder-alert.service` 发微信告警（见 [宕机告警](forwarder-alerting.md)）。
