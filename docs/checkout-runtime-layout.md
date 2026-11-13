# Git 工作副本与私有运行资料

源码以 `MukPun-WeChat` Git 工作副本为唯一开发入口。更新应提交并推送后由服务器 fast-forward 拉取，不再手工维护另一套源码目录。

| 位置 | 内容 | Git |
| --- | --- | --- |
| `scripts/`、`wechat_bridge/`、`tests/`、`tools/`、文档 | 可公开、可审阅的源码及合成测试 | 提交 |
| `.venv/` | 本地 Python 环境 | 忽略 |
| `.runtime/forwarder.json` | 实例私有转发配置 | 忽略，0600 |
| `.runtime/assets/` | 已校验的 Native 二进制及受限辅助库 | 忽略 |
| `.runtime/verification/` | 同步生成的受限工作进程源码视图及 `onebot-env` | 忽略 |
| `.runtime/worker-manifest.json` | 工作进程代码摘要 | 忽略 |
| `.runtime/migration/`、`.publish/` | 实例迁移、回滚、发布暂存资料 | 忽略 |
| `/var/lib/astrbot-wechat-session` | 根私有完整微信资料、快照、审计及预算 | 项目外，禁止提交 |
| `/var/lib/astrbot-wechat-bridge` | OneBot 身份、队列、游标及动作幂等状态 | 项目外，禁止提交 |

`.runtime` 不应整体挂载给 OneBot。它只读挂载经过核对的 `verification` 视图，不能读取实例配置、会话或 Native 密钥。Native 仍使用原有系统私有状态位置，迁移源码目录时无需搬走或重新生成这些资料。

## 更新流程

1. 本地修改、运行适用测试并检查待提交清单，提交源码。
2. 服务器在同一仓库 `git pull --ff-only`，确认目标提交。
3. `sudo python3 -I -B tools/sync_runtime.py --apply` 同步工作进程视图；`--apply` 不删除或覆盖 `onebot-env`。
4. 先确认现有桥无不确定发送、队列可安全交接，再重启本项目桥。修改 Native 所有者、资源上限或会话代码时还需单独受控保存、停止、恢复完整会话；不要把桥重启当作 Native 已更新。
5. 验证实际 unit 启动路径、代码摘要、SSE/反向 WS、游标、队列及回执。未知发送保留核对，不自动重发。

## 旧实例迁移顺序

先克隆 GitHub，再迁移私有配置、校验过的二进制和依赖；对配置和资料做字节摘要核对。已有根私有会话保持原位置，迁移前保存一致完整快照。

旧服务继续运行期间，在新 Git 目录做协议模拟、真实工作进程隔离自检，以及会话副本的断网恢复检查。不得让同一账号的两套客户端同时联网，也不得把验收消息推送到真实 AstrBot 以免额外调用模型。

以上通过后才停止旧桥并保存最终会话，切换本项目 unit 到 Git 目录的正式入口，恢复原账号和原有 OneBot 状态。失败保留原代码、unit 和完整资料，先恢复核心文字转发；不动 Docker、AstrBot、FRP 或宿主网络。验收前不删除旧实例，兼容路径仅用于回退或已有进程，不再作为独立开发树。

## 本次验证记录

2026-10-28 已按上述顺序完成准备与正式切换。新 Git 目录运行 701 项本地测试（Windows 跳过 29 项）及 291 项 Linux 隔离测试；已有账号的完整会话副本在断网环境恢复成功，API、存储及游标密钥摘要一致，没有第二套客户端联网登录。

实际切换前备份了最后一致会话、OneBot SQLite 和原 unit；切换后核对两项正式入口均位于 Git 工作副本，SSE/反向 WS 就绪，收到的实际消息继续转发，没有不确定记录。Native 的 128MiB 存储与图片源 8MiB 上限一并生效，Docker/FRP 业务进程及宿主网络基线一致。

本地旧开发树保存在 `.runtime/legacy-source/`，旧路径仅保留指向新 Git 目录的兼容联接。服务器旧部署在稳定性复查通过后整体归档至 `.runtime/legacy-deployment/`，旧路径仅作回退兼容。系统根私有状态保持原位置；这些目录均不得提交 Git。


## 显式包与角色清单（模块化 Task 1）

`PUBLIC_CODE`（`scripts/runtime_manifest.py`）是唯一公开源码交付 inventory，包含登记的嵌套包和 `__init__.py`；同步不再扫描运行/暂存目录。添加模块时同步登记，并运行 `tests.test_project_layout` 与 `tests.test_architecture_foundation`。`NATIVE_SOURCES` 给出受限目标名与源码路径，控制器打包和 Native `CODE_FILES` 同源；当前 Native 仍是 `/opt` 平铺，后续包依赖要显式扩展角色映射。host 角色列出 OneBot 所需包与 worker 文件，保持现有只读挂载与凭据隔离。

`--apply` 清理上一代 `worker-manifest.json` 中已不再交付的公开代码，保留 venv/data；未知文件不自动删除。所有源与目标（含父目录）及旧清单路径先核对，拒绝符号链接与越界路径；不得把暂存/私有文件手动加入清单。根 `run_onebot_tests.py` 仍是现有脚本的同步别名。


## 当前模块化交付与隔离验收（2026-11-13）

Tasks 1–5 的正式子包/调用链和兼容入口见[当前架构交接](modular-architecture-handoff.md)。`NATIVE_SOURCES` 已包含 wechat_native/wechat_sse、health 和 worker_failures 的显式 flat 映射；host 交付嵌套包，启动时核对 protocol=2 / 完整角色源码摘要与摘要绑定 ACK。源码清单不是 Native profile/凭据清单，不能为解决导入而扩大挂载。

`accept_reboot_recovery.stage_tree` 同样只复制 `runtime_manifest.public_sources()`，包括根 runner 别名，沿用同一 symlink/私有路径/缺失源码保护；不递归收集 scripts/tests/tools 下的任意 `.py`。失败删除半成品，目录 0755/文件 0644 供受限 DynamicUser 读取，原私有数据及隔离预算不变。本轮 isolated 尝试被 sudo 密码要求阻止，未创建 unit；历史 2026-10-28 的实际切换证据不能作为此次模块化部署或验收结果。
