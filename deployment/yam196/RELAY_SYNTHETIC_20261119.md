# YAM Relay 隔离公网模拟测试（2026-11-19）

后续定位已增加逐段抓包与局域网镜像，见[缺包定位报告](RELAY_DIAGNOSIS_20261119.md)。本文保留最初三轮结果，不代表最终定位结论。

结论：UDP5004/5005 双向路径可达；三路假视频可编码、转发并返回网络 ACK。但最终复测存在明显 PMRS 回包缺口，Relay ingress ACK 未实现。本阶段没有通过稳定性验收，也不能据此启用真实 Teleop。

## 测试范围

- Windows → 191.222.219.43:5005 → .196 内存模拟端点 → Relay → Windows，使用现有 PMRC/PMRS v3 编解码、Windows client bridge、已安装 server bridge 的纯解析函数。
- .196 已安装 sender 的 `fake libx265` 入口 → Relay UDP5004 → Windows 三个独立 UDP 接收端；160×120、10 fps、100 kbps/路，约15秒。
- Windows 对 RTP marker 时间戳返回模拟 NETWORK ACK；未发送 DECODE ACK，未进行 HEVC 解码、显示或 Operator EXE 验收。
- 每轮随机 `syn_<20hex>` 前缀，臂/head/left/right/ingress/隔离监听各自独立 session，均不使用真实站点 session；本次命名未覆盖 Relay 的 `arm` 前缀优先队列语义。
- .196 代码通过 SSH stdin 在内存执行，禁用 pyc，未上传文件、改配置、启停服务或访问真实相机/CAN/Mate。32秒远端 watchdog，子进程结束后复核。

## 三轮证据

| 轮次 | 独立前缀 | 结果 |
|---|---|---|
| 1 | syn_0b8503d309614ac6a767 | 三路假视频/网络ACK成功；server bridge 子进程先因 `yam_deploy` 导入路径缺失退出，臂测试无效。进一步静态确认其公网 main 要求 `require_running`；未补环境或放宽该门禁。 |
| 2 | syn_68ae43976ded4643b84a | 改用纯协议模拟端点。发送239、.196收到237、Windows返回225，RTT中位31ms；末尾238/239撞到退出边界，不把这两包计作网络丢失。 |
| 3（最终） | syn_61aa9a48601842018d11 | 修正累计定时误差，发送240、.196收到240、Windows返回146（60.8%）；脚本80%回包粗筛失败。该粗筛仅用于本次诊断，不是机器人安全验收标准。 |

最终轮成功回包的 RTT：最小15ms、中位31ms、P95约63ms、最大109ms。缺失回包不包含在 RTT 分布中，不能用这些时延掩盖回包缺口。

| 最终轮视频角色 | Windows RTP包 | 不同RTP时间戳 | 收到发送端统计包 | Windows发送NETWORK ACK |
|---|---:|---:|---:|---:|
| head | 253 | 106 | 51 | 12 |
| left | 244 | 104 | 50 | 11 |
| right | 245 | 103 | 50 | 11 |

三路 sender 的最后周期统计均编码140帧，最终退出日志各149帧；均观察到 `net_acked>0`、`relay_ingress_ack=0`。RTP时间戳数不等同于完整解码帧数，未据此计算视频帧丢失率。独立隔离监听 session 收包0，未发现跨会话流量。

最终轮额外发送130次仅包含合成RTP的 ingress 探测，返回 ACK=0；此前两轮分别125/127次，也均为0。结合云端正式 `relay_proto.h` 没有 type4、RTP分支没有发送该回执，可认定当前部署不提供新 sender 所需 ingress ACK 能力。接收端 NETWORK ACK 与 Relay ingress ACK 是不同机制；前者可用不能代替后者。

## 定位边界与下一步

- 最终轮 .196 已正确解码全部240个命令并调用状态发送；Windows client bridge 自身只转发146个状态，无 source/session drop 或 UDP reset 计数，缺口位于其公网接收之前。现有观测不足以区分 .196→Relay、Relay内部或Relay→Windows哪段丢失。
- 云端默认日志未开启逐会话 debug 计数，无法从现有日志补出每个PMRS的收发路径。本次未改日志级别、未抓生产流量、未重启服务。后续需要同步观测三段计数/抓包再归因，不能直接认定Relay代码导致丢包。
- 两端本次使用相同公网出口IP；流量确实经云端Relay，但未验证异地独立 NAT 场景、持续负载或真实60Hz遥操作。
- 完整已安装 server bridge main、真实 server、State 注册认证、Operator EXE、Mate映射、真实相机、运动和Home均未验。
- 若需修复 ingress ACK，应基于云端当前正式源码增加保留 DIR/FED 的最小、限频回执补丁，再验证与制定回滚；不能用本地缺少这些扩展的 relay.c 整包替换。本阶段仅完成评估，未生成或应用云端补丁。
- 新增远端写入/上传、生产更新、服务启停或设备操作仍须提供完整命令并逐次确认。

## 清理与保护状态

三轮记录的10个远端子进程PID均已消失，本地测试进程均已退出。sender在SIGINT清理后返回1，日志有最终编码统计；这不是自然退出成功码，证据保留原值。云端测试receiver目标已按10秒心跳超时失效，未修改其内部session表或重启服务强制删除条目。

最终只读复核通过：.196 GUI PID1104813、web PID1104912，policy active/exited；5个新Teleop unit仍disabled/inactive。station.json SHA256仍为 `bb4522ee89cabcf069ef94e3e770fb58070a3087e3c57f82ee96dc4fb4187007`。Relay PID仍721/722，正式二进制SHA256仍为 `1c2fdfe90f869f5f7f18a6fe6e98f3f5e7211c3415e4f9e97bdb37fb0f6b2506`。

原tracked代码无diff，无Git分支/commit/push；新增本地测试脚本与报告，未改动既有部署文件。密钥未读取内容或打包。

## 复现与本地证据

`python -B deployment/yam196/verify_relay_synthetic.py` 仅本地预检。带 `--run` 才会发公网测试流量，须处于已授权隔离测试范围。脚本/endpoint均通过AST与本地预检；最终公网执行如实返回非零。

- `.build/relay-synthetic-syn_0b8503d309614ac6a767.json`
- `.build/relay-synthetic-syn_68ae43976ded4643b84a.json`
- `.build/relay-synthetic-syn_61aa9a48601842018d11.json`（最终失败结果，含命令序列、时延、原始有限日志与前后状态）
- `.build/relay-synthetic-postcheck-syn_61aa9a48601842018d11.json`（最终保护/清理复核通过）
