# 解除私聊灰度的群限制

用户确认私聊正常并明确要求解除群聊限制。沿用现有全群能力，只把实例privateOnly从true改为false，allGroupsEnabled保持true，绑定私聊、五媒体能力、群前缀/真实@和其他预算保持。全群消息逐条交接AstrBot，由其唤醒策略决定是否回复；不改AstrBot配置，不枚举群目录，不补播此前被私聊模式跳过的消息，也不主动发送测试。

当前warm-only guard仍强制privateOnly=true，因此须同步把该精确条件改为privateOnly=false且allGroupsEnabled=true；其原owner/Native校验、禁止prepare/launch和GET在线证明保持逐字节不变。先只读预检、备份当前配置/guard/恢复证据，再仅停止桥。停止回执即使重现已知native_changed，也只在精确保存本次原始回执、所有worker/cgroup退出、原Native/owner/账号/真实锚点/审计/账本完整通过且journal仍ready时允许续接；身份、清理、持久证据或journal失败则保持停止，不删状态强开。

停稳后互斥下建立新清单，提交回调须接收manifest，保护下界仅按原捕获实现推进。完整审计与历史未知保持；配置和guard改动先比对完整旧字节、只替换上述条件并保留权限，原入口单次启动桥。验收实际privateOnly=false/allGroups=true、Native/SSE/WS/身份/ready、代码视图与捕获闲置；不把无真实群消息的检查称为群实测。旧一次性工具不执行，本次工具失败和阶段证据保存在root私有目录。

本轮不修改功能源码，仅复用8项既有全群/原生@/防循环/图片合成验证，追加必要运维门控检查和独立工具复审。开发、设计、灰度与AI接手文档同步实际结果。

## 实际执行与交接

8项现有针对测试3.866s通过，13项停止/guard纯门控检查通过，限定工具静态复审批准。只读预检为桥ready、queue2887 forwarded、历史未决54、恢复准入5/ledger revision5；用户已确认私聊正常，不额外发测试。

实际停止回执为stopped/ok=false、requires_review=false/uncertain0，cleanup/native/宿主namespace/network/business全部true。工具按原门控停在配置修改之前，完整回执、堆栈及备份保存于root私有all-groups-enable-20261116；精确诊断确认所有旧worker/cgroup退出、原owner/Native保持、全部baseline和文件无变化。没有宣称clean stop或根因修复。随后增加仅绑定这次已保存回执的resume-stopped：30分钟、source/owner/PID/ticks/完整baseline/旧配置/guard/队列及GET检查全部通过，12项拒绝路径与增量复审批准；唯一完成标记阻止再次消费，不把stopped/ok=false加入默认放行规则。

续接没有再次stop；通过现有capture发布新清单后，只改privateOnly true→false，allGroupsEnabled仍true。guard只替换精确模式断言，新SHA256为da2f6180fe5db5d377ce5a6395b568f39b81ee5eaf381cbdf165393e37971b26，原Warm/禁止prepare-launch/GET在线/owner身份部分保持。原入口单次启动，其他临时Restart/失败通知/维护策略保持，未改AstrBot、Docker、FRP、宿主网络或功能源码。

最终现场核验age0s，bridge/owner active，原Native/owner、ready/SSE/WS/自身身份均true；group_routing=true、五媒体开关保持，capture_phase=null/recovery=ready、捕获unit/PID/cgroup闲置、无pending/review。264文件视图changed0/removed0，queue2891 forwarded、历史未决54，恢复准入5/ledger5。正常群流量已有received/forwarded各4、成功回复动作3，私聊本代0；未主动发测试或重放旧消息，已有群流量按正常业务交给AstrBot，因此不能写成“模型调用为0”，也不能把回复动作数当模型调用次数。该观察不覆盖所有群、所有媒体来源或长期稳定性。

接手不要重跑本次apply/resume-stopped、旧私聊guard或旧一次性恢复工具。群由真实入站建立稳定路由，是否回复由AstrBot决定；私聊没有扩展为所有好友。旧native_changed及本次stopped/ok=false根因仍未定位，真实停机故障需另行处理。

开发/进度/灰度/AI入口已同步，公开审计356文件、零发现，git diff --check通过。本轮仅文档本地提交与正式副本同步，不推送GitHub、不为文档再次重启服务。
