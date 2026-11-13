# 私信灰度记录（2026-11-13）

## 已授权范围

仅现有绑定联系人；所有群入站/发送关闭。用户发起1条短文本，最多1次简短模型回复；本轮不测试图片、语音、表情或链接，不改 AstrBot/Docker/FRP/宿主网络，不重启 Native 或重新登录。

## 已完成源码与验证

privateOnly 默认false且兼容旧配置；Native、host/core和IPC三处限制目标。正式身份、持久游标锚点、恢复账本与activate保留。历史群身份种子只读核验不会授予群投递权限。开发/设计/交接已同步。

6项定向验证0.434s通过，相关84项1.863s通过，旧配置缺allGroupsEnabled覆盖privateOnly问题新增断言先失败再修复通过。静态复审发现并修复两项问题：旧字段补齐覆盖privateOnly（P1），以及部署工具写配置前未先拒绝.runtime父链符号链接（P2）。后者增加1项合成拒绝用例，先失败后通过；最终复核无剩余Critical/Important。全量1232项仍引用0452ba7，不重复宣称是本轮新跑。

## 真实只读预检

已取得用户 sudo 授权，仅通过关闭回显的标准输入使用，未保存凭据。通用root shell曾被自动审批拒绝（权限过宽且当时兼容缺陷未修复），未运行；修复后改用限定的单次只读命令获准执行。

原正式桥failed，停桥原因previous_image_send_requires_review。Native保留原进程，账号online且authenticated，剩余租期超过5天；首次探针未通过，后续明确GET在线证明通过。桥队列2860条forwarded，无queued/publishing/uncertain。53条旧桥动作结果未决（26无result、27ret1408），保留不重发；最新文本/语音accepted，图片unknown但audit_cleared=true、隔离记录仍在。selected_pair与_admit真实证明通过，ledgerRevision=3、3项Native例外已逐条覆盖。上述53项桥动作不等于53项新增Native未知提交，不能清理或当作重发许可。

## 切换状态

部署工具已准备，正式副本尚未切换；没有发送测试消息或调用模型。配置/unit备份与实际源码版本、SSE/WS就绪、消息结果将按实际操作追加。启动失败保留privateOnly、审计和队列，不自动回退到群模式或重发。只有真实ready之后才通知用户发送授权的那条私信。
