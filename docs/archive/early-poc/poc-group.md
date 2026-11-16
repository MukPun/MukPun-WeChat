# 专用测试群接收、识别和单条发送

测试群为“测试群”。群内随机标记接收、会话绑定及定向元数据补齐已实测通过。获准的一条群发送已得到微信服务器accepted，用户随后确认“已在群内收到”，目标群文本收发闭环验证通过。模型和回复策略继续由AstrBot负责。

## 已通过的真实验证

- 精确匹配另一位群成员发送的新随机标记，确认来自实时群消息；仅保存群会话绑定，不公开真实群ID或成员ID。
- 从完整保存资料自动恢复连接，无新二维码、无额外connect或initialize。
- 仅刷新已绑定群一次，结果succeeded、完整投影和准确群名核对均通过，随后保存完整SDK快照及私有验证记录。
- 无搜索条件重新读取本地群目录，得到一个具名项“测试群”。目录先前为空是元数据未投影，不能据此认为中文搜索有问题。
- 检查结束仅移除新检查进程，原生微信会话继续保留。宿主命名空间、网络和Docker/FRP进程基线检查通过。

## 已获准并执行的单条发送

固定目标为上述已验证绑定，固定文本为：

> 微信接入测试：这是一条群聊发送验证消息，无需回复。

按照此前发送前确认文本的约定，用户明确回复“允许，仅发送这一条”后，已对该群及该文本执行一次。该授权已经用完，不再重发。此前向“测试联系人”的私聊授权也已经用完，不适用于群发送。准备和上传程序本身均不触发真实发送。

发送前重新按完整群名查目录，要求唯一匹配且群ID与保存绑定一致；持久化root私有的一次提交记录、固定幂等键和文本摘要，再允许受限工作进程访问API。仅一次POST `/sends`，之后只查询对应send UUID；超时或结果不明不重发。任何已有本次发送记录都会阻止重新提交。

成功必须校验账号、目标群、发送ID、clientId、serverId和accepted状态，再保存SDK快照及私有发送结果。accepted表示微信服务器接受，实际到达仍需用户在群内确认。pending/needs_review等状态也会保留记录；恢复时既有outbox检查阻止未完成发送被重新执行。

发送程序与目录检查程序复用同一原生连接和私有网络。工作进程仍受32MiB、0swap、4tasks限制，与原生端共享128MiB/20%CPU/32tasks父slice；不获得Docker或FRP管理能力，不改变宿主内核、防火墙或路由。

相关代码为scripts/group_send_protocol.py、scripts/group_reader_client.py及scripts/run_group_probe.py。命令行发送模式必须带专门的--approved-single-message参数；该参数只供已获人类确认后使用，不能代替确认。普通列表、接收、绑定、刷新和会话恢复命令均不会调用发送。

## 2026-10-25 发送前验证

完整合成测试434项在Linux全部通过；Windows同一测试集通过，10项POSIX测试按平台跳过。覆盖固定群与文本、一次提交、固定持久化幂等键、超时不重发、错误目标或回执拒绝、已有发送记录阻止再次提交、无明确确认时在预检前拒绝，以及私有send UUID不转发到公开输出。集成测试核实发送记录先保存再允许工作进程请求API，accepted结果须在SDK快照提交后才保存为完成。

部署后另执行一次真实只读群识别。新工作进程的10项隔离检查全部通过，target_matched、group_result、group_probe_result均成功，最外层退出0。工作进程清理成功、原生端身份未变化，宿主命名空间/网络及Docker/FRP进程基线全部通过。该验证没有调用群发送，也未重新扫码。

## 后续验收

单条群发送得到accepted后核对群内实际到达。收发闭环通过后，再实现独立OneBot v11桥，与现有AstrBot做限群联调；接入现有AstrBot前先列出需要新增的适配器配置。

## 2026-10-25 真实群发送结果

发送前核实在线会话accounts=1、connected=true，restored=false、new_qr=false，复用同一个原生端，无需重启或扫码。重新核对群目录唯一匹配和私有元数据验证记录后，状态依次为target_matched、submitting、queued、accepted。

group_send_result为ok=true、submitted=true、server_confirmed=true、code=ok；group_result和group_probe_result均成功，最外层退出码0。完整SDK快照已提交，私有group-send-attempt.json中的accepted结果、准确目标群及固定文本摘要核对通过；真实群ID、send UUID和幂等键未输出或保存到项目中。

新工作进程隔离检查10项通过，清理后native_session_unchanged、reader_unit_removed、host_namespace_unchanged、host_network_unchanged、business_processes_unchanged均为true。没有重发或发送第二条，没有重新登录，现有Docker/FRP和宿主网络基线检查通过。

本结果证明微信服务器接受这条群消息。用户随后明确回复“已在群内收到”，实际到达已确认，目标群文本收发闭环验证通过；这不代表每位成员已读。该次授权已使用，不再次发送。AstrBot桥接仍未执行。
