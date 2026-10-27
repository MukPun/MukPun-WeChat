# 已停止的宿主模块方案（历史记录）

状态：2026-10-24 用户明确选择“保持宿主内核不变，改用用户态方案”。本方案不再执行，禁止运行下文的加载命令；保留内容只用于说明曾评估的变更范围。三个模块均未由本项目加载。

2026-10-24 实机只读预检已通过：`stage=check_complete`、`ok=true`、`changed=false`，下列三个目标的 `live` 均为 `false`。完整项目 127 项离线 unittest 通过，其中模块预检与失败处理覆盖 15 项；这些测试没有执行实际加载。

当前宿主为 `6.8.0-139-generic`，已有 `nf_tables`、`nfnetlink`、`nf_conntrack`、`nf_defrag_ipv4`、`nf_defrag_ipv6`、`libcrc32c` 处于 live。`nft_ct`、`nft_limit`、`nft_quota` 配置为模块但未加载，因此现阶段不运行任何可能触发自动加载的 nft 命令，包括 `list` 和 `-c`。

拟新增的全局内核注册状态仅限以下三个已安装模块，顺序为 `nft_ct` → `nft_limit` → `nft_quota`：

| 模块 | `.ko.zst` 字节数 | 压缩文件 SHA-256 |
| --- | ---: | --- |
| nft_ct | 16137 | `691ce7ddf140203cc4c5593d56ab780f2d354f0a909baa0d81d7f0907e72de7f` |
| nft_limit | 8030 | `aaa3943386a0a4ec3f0b353fcfde676b7abd7b7af6cdee852b6ed6e2e3143153` |
| nft_quota | 6733 | `de3c9b9e84c5b75e5b8b211881c28f081e5abd0eb9d646c5e13ae236eec0bf33` |

文件路径固定为 `/lib/modules/6.8.0-139-generic/kernel/net/netfilter/模块名.ko.zst`。模块成为 live 后对整台宿主可见，会增加全局内核代码、数据和表达式注册；网络命名空间不能隔离这种加载状态。此步骤本身不创建 nft 表或规则，不改宿主防火墙、路由、Docker、frpc，不安装包、不重启、不写自动加载或其他持久配置。

## 可先执行的只读检查

在 Linux 上审阅过的同版本项目副本根目录运行：

```sh
/usr/bin/python3 -I -B scripts/prepare-network-modules.py --check
```

省略参数也执行只读检查。它核对精确内核版本与架构、三个 `CONFIG_*=m`、文件 root 所有且非符号链接、不可被 group/other 写入、固定压缩大小及 SHA-256、模块名、完整 vermagic 与当前已 live 的 `nf_tables` 安装文件一致，以及直接和固定传递依赖均 live。它使用已安装的 `modinfo` 和只读 `modprobe --show-depends`；不调用加载模式或任何 nft 命令。

加载计划只允许本次目标及上表所需且已 live 的固定依赖。额外模块、install 指令、模块参数或目标缺失均导致失败。`--use-blacklist` 保留宿主管理员的 blacklist；目标未加载且被 blacklist 跳过时不会绕过或修改配置。工具环境不传递 `MODPROBE_OPTIONS`，也不输出配置中的原始指令。

只读加载计划还包含两个已编入当前内核的条目：`builtin crc32c_intel`、`builtin crc32c_generic`。它们没有 `/sys/module` 目录属于正常情况，不能据此当作待加载模块。脚本必须同时核实 `CONFIG_CRYPTO_CRC32C_INTEL=y`、`CONFIG_CRYPTO_CRC32C=y`，以及 root:root、不可被 group/other 写入的普通非链接文件 `/lib/modules/6.8.0-139-generic/modules.builtin`。该文件固定大小 9714 字节、SHA-256 为 `508ae45959f569e45b1e6f1888b254cdc02da5c4bb8b3edc043539b6dd2d735d`，并必须恰好各包含一次以下完整行：

```text
kernel/arch/x86/crypto/crc32c-intel.ko
kernel/crypto/crc32c_generic.ko
```

`libcrc32c` 仅作为必须已经 live 的既有依赖，不进入加载目标列表。它必须为 `CONFIG_LIBCRC32C=m`，文件 `/lib/modules/6.8.0-139-generic/kernel/lib/libcrc32c.ko.zst` 必须 root:root、0644 等不可被 group/other 写入的普通非链接文件，固定大小 2531 字节、SHA-256 为 `2fa4edc78171d4bb93cf9d0ee70f1d51a25b599c18f6e41824339bf2b23e4a25`，模块名为 `libcrc32c`、depends 为空且完整 vermagic 与当前内核依赖一致。任何其他 builtin、insmod、参数或重复计划条目仍被拒绝，不扩展到其他 CRC 算法或模块。

## 需要用户另行明确同意的加载范围

确认后才允许 root 执行：

```sh
sudo /usr/bin/python3 -I -B scripts/prepare-network-modules.py --load-if-approved
```

这个参数是操作者对已经取得确认的显式声明，不会自动取得用户授权。脚本会先重复全部检查，并在每个模块加载前再次检查。

三个模块分别在一个全新随机名称的 systemd 短服务中顺序执行以下固定命令；没有 shell、参数扩展、force 或 remove：

```text
/usr/sbin/modprobe --use-blacklist --ignore-install nft_ct
/usr/sbin/modprobe --use-blacklist --ignore-install nft_limit
/usr/sbin/modprobe --use-blacklist --ignore-install nft_quota
```

systemd 在启动服务入口前通过 `UnsetEnvironment` 移除 `LD_PRELOAD`、`LD_LIBRARY_PATH`、`LD_AUDIT`、`GLIBC_TUNABLES`、`PYTHONPATH`、`PYTHONHOME`、`MODPROBE_OPTIONS`，防止它们先影响 `/usr/bin/env` 自身。服务入口再使用固定 `/usr/bin/env -i` 清空继承环境后 exec 上述命令。`--ignore-install` 禁止目标的 install 替代命令；由于依赖仍可能受配置影响，只读计划检查还会拒绝依赖中的 install 指令和额外 softdep 模块。[kmod 参数与依赖说明](https://man7.org/linux/man-pages/man8/modprobe.8.html)、[配置语义](https://man7.org/linux/man-pages/man5/modprobe.d.5.html)

每个服务为 `User=root`，bounding set 仅 `CAP_SYS_MODULE`，不授予 `CAP_NET_ADMIN` 或 `CAP_SYS_ADMIN`；启用 NoNewPrivileges、独立私有网络、私有设备、只读文件系统、隐藏 home 和关键 Docker/containerd socket 路径。限制为 CPU 20%、100ms period、MemoryHigh 24 MiB、MemoryMax 32 MiB、swap 0、TasksMax 8、RuntimeMaxSec 10、TimeoutStopSec 2；不自动重启。这里限制的是加载器用户态进程及其可计费内存，不宣称新模块的全局内核内存也受 32 MiB 限制。`ProtectKernelModules=yes` 与实际加载操作冲突，因此此专用服务不启用它。

每次命令结束后必须看到 `/sys/module/模块名/initstate` 为 `live`，否则停止后续模块。已经 live 的模块只记录并跳过；完成结果的 `loaded_by_this_run` 列出本次成功执行加载并确认 live 的模块，`global_kernel_state_changed` 根据该列表是否非空输出。如果三个模块均被跳过，变更标记为 `false`。脚本只停止自己创建的随机 unit，并确认其 inactive/不存在、MainPID 为 0、已知 cgroup 无进程；SIGTERM、SIGINT、SIGHUP 也进入 finally 清理。清理不会卸载任何模块：前面已成功加载的模块可能被后来使用者引用，自动卸载可能影响其他工作负载。失败时保留已经发生的全局状态并报告安全原因，由人工审阅后另行决定处置。

`failed` 只表示整个步骤未通过，不代表内核未改变。失败输出包含 `initial_live`、`current_live` 三模块状态（`live`、`not_live` 或读取失败时的 `unavailable`）、固定目标名称组成的 `attempted` 和 `loaded_by_this_run`、`possible_global_kernel_state_change`、`cleanup_unconfirmed`。加载成功并观察到 live 后立即记录进度，早于 finally 中的 unit 清理；因此即使随后清理失败，也不会抹去已经发生的加载。只要已尝试执行加载或前后状态不同，就保守报告可能存在全局变化。异常路径的状态读取失败只标 `unavailable`，不会覆盖原始安全失败原因。

三个模块全部 live 也只意味着可以继续设计受控 guest nft 实验，不能据此声称限速、配额、防火墙或微信登录已经通过。后续规则必须另外核验 target netns 的归属及 pin FD，只应用到该 guest；不修改宿主规则。
