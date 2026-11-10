# 中转层停摆告警

## 为什么需要

2026-11-09 02:35，一张图片发送结束后桥进程退出，systemd 重启 6 次全部失败后放弃。
从那时到 2026-11-10 15:54 被发现，**中转层停了 37 小时**。

停摆期间没有任何报错可见：桥是唯一和 AstrBot 说话的进程，它一死，AstrBot 那边只是
安静下来，看起来和"群里没人说话"完全一样。AstrBot 自己有平台超时和重连日志，但它
不会为了一个不再连上来的 OneBot 客户端去报警。

所以需要一条**独立于桥的通道**。桥死了，通道还活着。

## 通道选型

试过两条，第一条不通：

| 方案 | 结果 |
| --- | --- |
| AstrBot Open API `POST /api/v1/im/messages` 走 `weixin_oc` 公众号 | **否决**。`weixin_oc_adapter.py:907-916` 要求 `self._context_tokens[user_id]`，这个 token 只在用户主动给公众号发消息时由 `getupdates` 刷新。该会话最后活跃是 2026-05-06，实测返回 `ret=-2 prepare failed`。客服消息通道不能做长期无交互告警。 |
| 直接调用 Native daemon 的 `POST /sends` | **采用**。daemon 跑在独立的 transient unit 里，桥 failed 时它照常服务。 |

关键点：**Native daemon 的数据和网络都在自己的命名空间里**，宿主看不到它的 socket、
也读不到它的 token。所有调用必须经由 `nsenter`。

## 怎么工作

```
forwarder.service 失败
      │
      ├─ systemd 先自动重启（Restart=on-failure, RestartSec=30, StartLimitBurst=3）
      │
      └─ 超过 StartLimit → 真 failed → OnFailure= 触发 alert.service（oneshot）
                │
                ├─ systemctl show ActiveState → active？直接返回 already_healthy
                ├─ 扫 cgroup 找 /opt/wx 的 pid（不是单元的 MainPID）
                ├─ nsenter -m cat 读 api-token（只在管道里传递）
                ├─ GET /contacts?search=<privateContact> 精确匹配 displayName
                └─ POST /sends（idempotency-key 在 **header**）→ 微信私聊
```

### 三个必须记住的坑

1. **幂等键在 header，不在 body**。`SDK $CR/wechat-api/src/lib.rs:982-996` 读的是
   `headers.get("idempotency-key")`，body 里放没用，缺了直接 `idempotency_key_required`。

2. **daemon 的 pid 不是单元的 MainPID**。`astrbot-wechat-login-<id>.service` 的 MainPID
   是 `session_runtime_client.py`（PID 2419），真正的 `/opt/wx` 是它的子进程（PID 2422）。
   正确找法是读单元的 `ControlGroup`，再扫 `/sys/fs/cgroup<group>/cgroup.procs`，
   逐个 `/proc/<pid>/cmdline` 里找 `/opt/wx`。

3. **联系人搜索是 contains-match**。`GET /contacts?search=木栽盆下` 会返回所有名字里含
   这四个字的联系人。脚本在本地再做一次 `displayName` 精确相等，并排除 `@chatroom`
   结尾（群）和 `gh_` 开头（公众号）的 id。

### 为什么用 drop-in，不改 forwarder 单元

`scripts/run_forwarder.py` 的 `install()` 会校验单元文件逐字节等于 `unit_text()`，
任何手工修改都会被判为 `unit_definition_changed`。所以触发关系写在
`/etc/systemd/system/astrbot-wechat-forwarder.service.d/alert.conf` 里，不动原件。

### 为什么要有 `active` 守卫

`OnFailure=` 对**任何**失败都触发，包括运维手工 `systemctl stop`（`Result=exit-code`
但服务是被主动停的）和 `restart` 过程中的瞬态失败。这些不该打扰用户，所以脚本第一步
先用 systemd 的 `ActiveState` 判断，是 `active` 就静默返回。

注意这里读的是 **systemd 的状态**，不是桥自己写的 `/run/.../forwarder.json`——被杀的
进程不会更新自己的状态文件，那份自述会说 "active"。

## 安装

```bash
sudo python3 -I -B scripts/forwarder_alert.py install
systemctl show -p OnFailure --value astrbot-wechat-forwarder.service
# → astrbot-wechat-forwarder-alert.service
```

安装会写两个文件（`O_CREAT|O_EXCL|O_NOFOLLOW`、0644、单链接校验）：

- `/etc/systemd/system/astrbot-wechat-forwarder-alert.service`
- `/etc/systemd/system/astrbot-wechat-forwarder.service.d/alert.conf`

然后 `daemon-reload`。

## 验证

```bash
# 1. 健康时短路，不发消息
sudo python3 -I -B scripts/forwarder_alert.py
# → {"code": "already_healthy", "ok": true, "stage": "alert"}

# 2. 看要报什么（不发送）
sudo python3 -I -B scripts/forwarder_alert.py --render

# 3. 真实触发（会真的发一条微信）
sudo systemctl stop astrbot-wechat-forwarder.service
sudo systemctl start astrbot-wechat-forwarder-alert.service
sudo journalctl -u astrbot-wechat-forwarder-alert.service -n 3
# → {"ok": true, "sendId": "...", "state": "queued", ...}
sudo systemctl reset-failed astrbot-wechat-forwarder.service
sudo systemctl start astrbot-wechat-forwarder.service
```

真实触发的返回里 `state: queued` 表示 Native 已接收；几秒后查
`GET /sends/<sendId>` 会变 `accepted` 并带 `serverId`，那才是微信真的收下了。

## 配置

可选键 `alertThread`（写进 `.runtime/forwarder.json`）可以跳过自动解析，直接指定私聊
thread id（如 `angle894535259`）。不设则每次启动时按 `privateContact` 的名字去
`/contacts?search=` 里找。名字改了、联系人有重名，就设这个键。

## 边界

- **只报告 `failed`**，不做"进程活着但不健康"的持续轮询。桥内部已经有一套
  `recovery_coordinator`（DELAYS 1..30 秒、COOLDOWN 300 秒），进程内的重试它自己管。
- **所有异常路径都 `return 0`**。告警失败不能把一个 failed 单元变成两个。
- 告警走的是**当前登录的微信账号**，所以微信掉线时连告警也发不出去。这种情况下
  只能靠人工看 `systemctl status`。
