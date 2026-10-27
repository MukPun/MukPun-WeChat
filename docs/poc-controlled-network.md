# 有界联网实验

本阶段继续验证 Linux 原生微信端，不连接现有 AstrBot。前置的离线空账号启动已经通过，记录见 [空端实验](poc-empty-client.md)。

2026-10-24 用户决定保持宿主内核不变，停止模块加载及 guest nft 路线。后续已通过用户态网络完成真实二维码和测试账号手机扫码登录，返回 succeeded 后断开并清理，见 [登录实验](poc-login.md)。下一步为测试群识别、收发和 AstrBot 桥接。下文 nft 相关设计保留为历史记录，不表示后续允许执行。

## 实验边界

- 不操作现有 Docker API、容器、网桥、端口映射或配置，不管理 frpc。
- 不修改宿主路由、防火墙、sysctl、AppArmor 或全局 user namespace 策略。
- 不使用宿主网络直接运行微信端，不让网络辅助进程常驻为宿主 root。
- 每一步先运行可信系统工具与自检；身份、权限或资源门禁失败，就停止当前步骤。
- 只停止本次随机命名的单元和它们的子进程，不按程序名称批量终止。
- 不把 sudo 凭据、API token、微信登录资料或二维码写入项目、日志或上传材料。

## 分步验收

| 步骤 | 验证内容 | 可运行的程序 | 通过后支持的结论 |
| --- | --- | --- | --- |
| 2A | 创建归属独立动态用户的 user/net namespace，并完全降权 | 系统 unshare、setpriv、Python 自检 | 新网络空间与宿主不同，后续有条件尝试非 root 联网辅助程序 |
| 2B | 在目标网络空间创建用户态 TAP，出口默认禁止 | 系统 slirp4netns 与可信自检 | 辅助程序无需宿主 root；未改变宿主接口、路由或业务进程 |
| 2C | 仅允许本次解析的微信公网地址、限制端口/时长/资源，并验证拒绝策略 | 可信小型连接探针 | 限制实际生效，可进入二维码获取实验 |
| 2D | 固定版本原生端创建空测试账号，取得一次二维码后取消并销毁 | 已校验的微信端与白名单 API 控制器 | 只证明新资料的二维码流程可达，不等于已登录或群聊成功 |
| 2E | 用户使用专用测试微信扫码，观察登录结果后立即断开 | 同上 | 只证明测试账号能登录，不等于群收发已验证 |

2B 以后需要对辅助程序与客户端设置一个共同的资源上限，不能把多个独立单元的上限误称为总量。具体运行值与峰值按实测补充。

## 候选网络设计

系统已有 slirp4netns 1.2.1。它通过用户态网络转发连接，无需新增宿主 veth、桥接、NAT 或路由；具体权限和沙箱兼容性仍须逐步验证。

受信 bootstrap 仅在创建 user/net namespace 时短暂使用 CAP_SYS_ADMIN。进入新的 user namespace 后，用系统 setpriv 清空 effective、permitted、inheritable、ambient 以及 bounding capabilities，才执行自检脚本。内部 UID 0 映射到宿主非 root 的 DynamicUID，不能用内部 UID 直接判断宿主权限。根控制器从宿主检查 UID 映射、namespace owner、父 user namespace 及网络 namespace 的归属；客户端本身不获此 bootstrap 权限。

后续辅助进程应以同一个非 root 宿主 UID、零宿主 capabilities 运行。`--disable-host-loopback` 单独不能阻止访问宿主其他地址、局域网和 Docker 网桥，所以不能作为唯一出口保护。候选方案同时使用目标 namespace 内默认拒绝的过滤，以及辅助单元的 IP 出口限制；两者都需要验证实际效果。禁止把修改宿主防火墙当作兼容性修复。

关闭 slirp 内置 DNS，给隔离端提供本次解析并校验过的只读 hosts 映射，避免内置 DNS 通过宿主 resolver 访问宿主服务。只允许公开的微信服务地址，排除全部宿主地址、私网、保留网段、FRP 接入地址和 IPv6；地区重定向若不在本次允许清单内就失败，不自动扩大目的地。

slirp 本身没有精确的带宽或字节硬限额。guest 包过滤和短时资源限制也不能宣称严格限制宿主物理网卡收到的全部字节；若增加流量监测，需说明采样间隔和可能的在途超量。

## 登录 API 控制范围

固定版本 v1.0.0-beta.8 使用本机 Bearer API，只允许本次账号的创建、扫码登录、指定 operation 状态/二维码、取消和断开。控制器不调用 chats、messages、sync 或任何发送接口。HTTP 不跟随重定向、不读取环境代理；JSON 与图片均设置响应长度上限，错误只输出固定错误码。

登录请求为 QR 策略、manual 刷新、maxRefreshes=0、timeoutSeconds=120。HTTP 登录本身返回异步 operation，不传不存在的 detach 字段。账号资料和 token 留在私有临时文件系统中；二维码只在可信显示通道中传递。

**上游限制：** 该版本登录成功后会立即启动后台 worker，可能自动同步。控制器可以在看到成功时立即断开，且自己不读取或展示聊天正文，但无法保证原版程序完全没有自动同步窗口。因此扫码应使用专用测试账号。若必须保证没有任何同步，需要先审查并修改上游实现，不能单靠调用方回避。

## 当前状态

已先单独验证辅助单元的 IP 拒绝能力（脚本 `scripts/probe-ip-egress-guard.py`）：

- 单元前缀 `astrbot-wechat-ipguard-92abdbc846d6`；先运行正向对照，再运行全部拒绝对照。
- 可信根控制器临时创建新的 127.0.0.1 和 ::1 随机 TCP 监听；没有访问任何现有业务端口或外网。
- 两个对照单元都通过实际 non-root、全部 capabilities=0、NoNewPrivs、根只读、用户目录隐藏和资源检查：32 MiB、swap=0、20% CPU、8 tasks，运行上限 8 秒。
- 正向对照的 IPv4、IPv6 都成功连接；相同沙箱启用 IPAddressDeny=any 后，两者都超时，并且监听没有收到受限单元的连接。
- 结果为 `IP_EGRESS_DENY_VERIFIED`。本次 systemd 构建显示 `-BPF_FRAMEWORK`，但这并未禁用已实测有效的系统级旧 IP 过滤能力；没有只凭构建选项推断。
- 两个单元均已回收，MainPID=0、LoadState=not-found、ActiveState=inactive；临时监听已关闭。
- 宿主 net namespace、IPv4 路由，以及 frpc、dockerd、containerd 与六个已知 docker-proxy 的 PID/启动时间与基线相同。

这只证明全部拒绝策略有效，不证明将来的目的地允许清单、TCP 端口过滤、用户态 TAP 或微信连接已经通过。

### 2A：独立 namespace 与降权通过

成功单元：`astrbot-wechat-owned-netns-c1a99994fca8.service`，结果 `OWNED_NETNS_VERIFIED`。

- 宿主 UID/GID 为 64114，内部 UID/GID 为 0，只有一条 `0 → 64114` 映射；补充组没有额外权限，setgroups=deny。
- keeper 的 effective、permitted、inheritable、ambient、bounding capabilities 全部为零，NoNewPrivs=1。
- 新 user namespace inode 为 4026532611，net namespace 为 4026532612；宿主 net namespace 为 4026531840。
- ioctl 验证 net namespace 的 owner 是该新 user namespace；user namespace owner 为宿主 UID 64114，父是宿主 initial user namespace（inode 4026531837）。
- 只有 lo，接口为 DOWN，IPv4 表没有路由；IPv6 仅有无 next-hop、不可转发的内核 ::/0 reject 条目。
- cgroup 验证 128 MiB 上限、96 MiB high、swap=0、20% CPU、16 tasks；单元运行上限 20 秒，可信 keeper 等待 12 秒后正常退出。
- 实验仅执行系统 unshare、setpriv 和只读自检 Python，没有启动 slirp 或微信。

过程发现并修复两处探针问题：启动阶段过早读取 `/proc/PID/exe` 会发生 ENOENT 竞态；隔离空间的 IPv4 路由表可以是完全空文件。现在先等完整的 keeper 就绪记录再验证 PID，IPv4 只接受空表或唯一的精确标准表头；任何真实或畸形路由仍被拒绝。没有扩大引导权限、增加资源或改变宿主安全策略。

失败尝试均停止并回收了自己的单元及临时副本。成功后没有保留 namespace、账号数据或常驻服务。

### 2B：用户态 TAP 创建与销毁通过

结果：`SLIRP_TAP_VERIFIED`。

- keeper：`astrbot-wechat-owned-netns-bd3338d32cb5.service`。
- helper：`astrbot-wechat-slirp-tap-helper-bd3338d32cb5.service`。
- 共同父 slice：`astrbotwechatprobebd3338d32cb5.slice`，实际读回总上限 128 MiB、high=96 MiB、swap=0、CPU 20%、32 tasks。两个叶单元不能突破此总额；可信根控制器本身在该 slice 外，未计入这个上限。
- slirp 实际宿主 UID/GID 与活 keeper 相同，非 root；五组宿主 capabilities 全零，NoNewPrivs=1，父进程留在原宿主 user/net namespace。
- root 与 launcher 分别固定 namespace FD，反复核验 inode、owner、进程启动 ticks；slirp 的实际 argv、继承 FD 和 cgroup 均符合预期。
- 只暴露已经存在的 TUN 字符设备 10:200；没有加载新驱动，没有配置 TAP、地址或路由，没有启动 guest 应用。
- slirp 关闭 DNS/宿主回环映射，启用 seccomp；系统级 IPAddressDeny=any，实际入站/出站 IP 计数均为零。
- 私有网络空间只有 DOWN 的 lo、tap0，没有 IP 地址或可转发路由。root 的只读 ip 查询只使用核验后的固定 namespace FD。
- 收到确切 ready 字节后停止 helper，确认其单元/cgroup 没有进程，再检查 TAP 已消失；随后停止 keeper 和父 slice。退出状态为 0，全部精确清理确认通过。
- 宿主 namespace、链路与路由配置、Docker/frpc 相关 PID 与启动 ticks 前后相同。IPv6 RA 的正数到期计时器自然变化单独记录，不掩盖网关、前缀、设备、路由增删或到期等真实变化。

### 本阶段的兼容性发现

1. 对未知 slice 使用 systemctl show 会自动合成已加载的 slice；预查改用不加载单元的精确 list-units。
2. 遮罩整个 /run 会使本机 systemd 单元在 226/NAMESPACE 阶段退出。可信四组挂载对照已证明：关键 socket 遮罩时有无 TUN 绑定均正常，整个 /run 遮罩时均失败。最终只遮罩 Docker/containerd/user/dbus/systemd private/FRP 等明确路径，保留 systemd 内部启动所需目录；系统仍只读。
3. InaccessiblePaths 的假 socket 仍可以具有 socket 类型；正确门禁是精确、只读的 systemd inaccessible 挂载、mode=000 和访问拒绝。不能要求其类型必须不是 socket，也不能仅凭路径存在判断业务 socket 可用。
4. 被隐藏目录的 stat 可以抛 PermissionError；该结果表示不可访问，不要求查询必须返回不存在。其他异常仍拒绝继续。
5. 根控制器增加 SIGHUP 清理处理；一次性 SSH 认证执行后立即失效并关闭连接。期间交互连接曾重置，重连均先复核关键服务并确认没有测试单元残留。

上述修正没有授予 slirp 宿主 capabilities、启动微信或开放 IP 出口。AF_UNIX socketpair 是 slirp 初始化所需；当前保护不应被描述为宿主抽象 Unix socket 的完全隔离。

以下为取得二维码前的历史检查：当时 2C/2D 尚未执行。后续用户态实验已建立微信原生连接并展示真实二维码，手机确认等待超时，详见 [登录实验](poc-login.md)。匹配内核 6.8.0-139-generic 的配置显示 nft_ct/nft_limit/nft_quota 为模块；其文件已经安装、依赖 nf_conntrack/nf_tables 已 live。项目仅只读运行过 modinfo 和 modprobe --show-depends，没有执行加载、运行 nft、安装软件或改写宿主防火墙。[模块方案](network-module-change.md) 已由用户否决，不再执行。

只读预检首次因未识别加载计划中的 builtin 条目而拒绝继续，没有改变模块状态。随后核实：libcrc32c 已 live，crc32c_intel 与 crc32c_generic 在本机内核配置中为 y，且存在于匹配版本的 modules.builtin。只允许精确识别这些既有依赖，不能借此允许加载任何额外模块。

修订后的 prepare-network-modules.py --check 实机返回 stage=check_complete、ok=true、changed=false；三个目标模块均为 live=false。全项目 127 项离线测试通过，包括 15 项模块预检、拒绝额外依赖与加载失败诊断测试。用户随后拒绝内核变更，实际加载不再执行。

2026-10-24 再次只读复核：frpc PID 1026310/start ticks 28011477、dockerd PID 1201/start ticks 1446、containerd PID 949/start ticks 1003，以及六个 docker-proxy 均与实验基线一致；没有本项目测试单元或 /var/tmp staging 目录残留。三个目标模块仍未加载；memory PSI 的 avg10/60/300 均为 0，累计值也未变化。这证明已核查进程没有重启和本次清理完成，不代替完整业务功能验收。

### 离线扫码协议库

已实现 `scripts/qr_login_protocol.py`，28 项 mock 测试通过。它仅允许本次账号的登录相关 API，支持取得一次 QR 后取消、断开的 `qr_only` 模式；后续真实网络、二维码与测试账号登录均已通过，见 [登录实验](poc-login.md)。库的协作式 deadline 不能抢占阻塞回调，外层硬时限仍由 systemd 单元负责。

## 核查来源

- [Linux 6.8 user namespace 创建与 capability 初始化](https://github.com/torvalds/linux/blob/v6.8/kernel/user_namespace.c)
- [user_namespaces(7)](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)
- [slirp4netns v1.2.1](https://github.com/rootless-containers/slirp4netns/tree/v1.2.1)
- [固定版本登录与 worker 实现](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-core/src/runtime.rs)
- [固定版本 MMTLS 网络实现](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.8/packages/native-client-rust/crates/wechat-protocol/src/mmtls.rs)
