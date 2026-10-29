# AstrBot / SoVITS 原生语音输出

常驻桥支持 OneBot `record` 消息段，向指定私聊或已允许群聊发送微信原生语音。AstrBot 在调用 OneBot API 前将 SoVITS 文件转换为 base64，因此桥不需要访问 AstrBot 文件目录，也不添加文件挂载。

## 输入与转换

- 一次动作包含一条 `record`，可附带同会话 `reply`；混合文字、图片或多条语音会整条拒绝。
- `data.file` 为 `base64://...`，原始音频不超过 8 MiB。拒绝本地路径、远程 URL 和额外参数。
- 支持 PCM 整数 WAV：8/16/24/32 位、单声道或双声道、8–96 kHz。SoVITS 常用的 32 kHz PCM16 WAV 会转换为单声道 16 kHz PCM16。
- 最长 300 秒；格式正确的长音频仍须符合来源字节上限。标准化数据最多 9,600,044 字节，通过带确认的 64 KiB 分块传输，不扩大 256 KiB 的 IPC 帧上限。
- Native v1.2.0 将音频编码为 SILK。60 秒以内发送一条原生语音；更长音频使用最少数量的等长语音片段，按顺序发送。

转换使用项目固定的 Python 3.12 标准库 `audioop`。Native 接收标准 PCM WAV，使用内置快路径，不需要安装 FFmpeg。

## 启用

在完成项目部署的实例中执行：

```bash
sudo python3 -I -B scripts/run_forwarder.py voice-output enable
sudo python3 -I -B scripts/run_forwarder.py restart
```

配置项为 `voiceOutputEnabled`，默认 `false`。`can_send_record` 反映此开关。配置生效需要重启桥，已有微信 Native 会话继续复用。

## 结果与恢复

桥先保存 OneBot 动作去重和根进程审计，再上传音频并提交一个带幂等键的 `kind: voice` 作业。成功要求每个分段均获服务器确认，且对应 type-34 消息、SILK/WAV 媒体缓存和官方写回全部校验通过。仅上传完成或部分片段成功，不会返回整条成功。

提交前确定失败会返回明确拒绝。提交结果未知时，保留审计和去重，停止继续发送并禁止自动重试。下一次启动在恢复 Native 之前检查语音审计，关闭语音开关也不能绕过未知动作。

语音与图片共用容量计算原则，为文字和后续快照保留空间；语音有独立预算计数，并额外预留 SILK 编码计划、WAV/SILK 缓存、官方写回及数据库副本所需空间。图片输出 8 MiB 与图片输入 128 KiB 的限制保持原值。

## 验证边界

自动化测试使用合成 PCM、模拟 HTTP 和真实工作进程的匿名管道，覆盖重采样、格式拒绝、动作去重、私聊/群聊路由、长音频分段、回执绑定、部分失败、资源拒绝和持续 SSE。此类测试不向真实微信联系人发送语音，不代表真实账号的交付验收。

## v1.2.0 的语音播放修复

原版 Native v1.2.0 的 `uploadvoice` 请求把字段 11（取消标志）写为 4、字段 13（编码格式）写为 0。实际数据为 SILK，接收端可能拿到语音消息却无法正常播放。本地 SILK 回解码、服务器接受回执和缓存写回均不能证明接收端可播放。

[上游修复 28f8ea85](https://github.com/thisnick/agent-wechat/commit/28f8ea85f08d2811b162dc84b228a3ab6c25f690) 将这两项分别改为 0 和 4，覆盖首段、续段和末段。本项目只回移该协议修正、合成线格式向量及分段断言，补丁位于 `patches/agent-wechat-1.2.0-voice-format.patch`，不引入该上游提交的其他功能。

使用 v1.2.0 发布提交 `33ec6b2cf42e364d8a30d907666de976ad9f6cd0` 的源码，在上游仓库根目录应用补丁，然后在 `packages/native-client-rust` 中构建：

```sh
patch -p1 < /path/to/agent-wechat-1.2.0-voice-format.patch
cd packages/native-client-rust
cargo test --locked --release -p wechat-protocol --lib long_voice_note_uses_consecutive_ranges_of_one_client_id
cargo test --locked --release -p wechat-protocol --lib metadata_matches_independent_synthetic_wire_vectors
cargo build --locked --release -p wx-native --bin wx
```

Rust 工具链固定为 1.93.1，依赖遵循原始 Cargo.lock。修正版仍报告 1.2.0，但资产名称包含 `voice-format`，与原始发布资产分别保留；启动器继续以修正版的精确大小和 SHA256 校验，不放宽二进制门禁。构建产物不随源码提交。

切换前备份完整保存资料和当前审计，只停止本项目的桥与 Native，并核对保存登录与未知动作在恢复前后保持一致。旧语音不会被修改或自动重发；补丁只作用于新提交的语音。客户端播放是否正常需由新语音的实际接收结果确认。

AstrBot 中 `gsv_tts 没有返回值，或者已将结果直接发送给用户` 来自工具返回 `None` 的处理分支。GPT-SoVITS 插件成功执行 `await event.send(...)` 后没有返回值，正会进入这个分支并结束工具循环。该 WARN 本身不表示发送失败，也不应通过改变返回值来触发模型追加或重复发送。
