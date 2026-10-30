# 高清图片输入实施计划

> REQUIRED SUB-SKILL: subagent-driven-development。使用现有隔离工作区；用户已确认约25MiB上限，继续完成实现与部署。

**目标：** 自动取原图，以原始字节交给AstrBot，兼容16MiB WebSocket边界。
**架构：** 精确Native媒体读取＋受确认的入站分块＋专用临时文件交付。

## 全局约束

输入25MiB/16384边长/100000000像素；输出8MiB与语音8MiB不变。IPC256KiB、块≤64KiB、仅一个未确认块，组装180秒、下载60秒。临时文件512MiB/256项/600秒、仅专用目录。登录、未知审计、去重、32条/2MiB队列和其他服务配置保留。

1. Native读取：scripts/image_native.py与独立测试；先失败测试原图pending且缩略图cached场景，再实现精确quality/assetId下载、标准回退、摘要/身份失败拒绝。运行该变化覆盖测试。
2. 输入载荷与分块：wechat_bridge/image_input.py增加上限与文件描述符校验；新增image_input_wire.py实现Producer、Receiver、Relay。原图完整性、确认时序、超时和多帧25MiB用合成测试覆盖。
3. 临时文件与host：新增image_input_files.py，Gateway在入队前保存/转换大图为固定文件描述符，周期清理；绑定专用目录并核对容器root读者。覆盖路径、SHA、大小、拒绝容量、期限及小图兼容。
4. 集成：Native主循环、root控制器、host接收分块与目录配置；同步256/512MiB内存及所有精确校验、容量门禁。测试合成CQHttp完整收到25MiB原图文件与文本兼容，不改AstrBot。
5. 审核与发布：只运行变化覆盖；保存root私有备份，短暂停桥安装源码/worker并复用Native，恢复桥与timer，读取已有图片的原图/标准/缩略图尺寸摘要，不补发旧消息。发布公开源码，检查GitHub与工作区一致。
