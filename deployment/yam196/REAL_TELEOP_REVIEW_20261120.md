# SZ_YAM02 真机联调准备（2026-11-20）

用户已暂停UDP丢包归因，选择直接准备真实Mate、三相机和双臂的端到端联调。本单是启用前审查，不代表设备已启用或正式链路已验收。

## 最新结果：YAM待机与真实公网视频已通过

用户随后明确回复“允许”，已实际执行待机部署。远端初次75测试通过、trial正常退出、原评测停止；首次视频启动失败且控制器全部清理并fault。对照发现私有SDK 2.58.4 RSUSB能枚举3台相机，但启动head640x480 YUYV30返回No device connected；原评测SDK同参数0.47秒成功。使用既有源码离线重编译私有SDK，FORCE_RSUSB_BACKEND改OFF，CMake确认V4L2；旧SDK备份至`backups/standby-20261120-9341e2fe64/realsense-rsusb`。不改原评测环境、不下载依赖、不改USB权限。install.py同步修正以保证后续构建可复现。

新增显式`retry-standby`，只允许fault且所有评测/Teleop单位停止、无残留占用时重试，保留原previous_eval。追加测试后远端80 passed；本地79 passed、1 Linux锁跳过。重试代码备份`backups/standby-20261120-9341e2fe64/retry-code`，之后retry-standby实际返回0。

当前Agent及三路sender active，原web/policy/gui inactive，trial无残留；mode=standby，State connected/synced=true，正式快照snapshot.agent_online=true。能力关闭、无session/Home/ArmStack，enabled=false、commissioning全false、Home/mapping仍空。三相机约30fps持续编码，10秒帧数增量head295/left302/right301，证据`.build/standby-readiness-20261120.json`。

Windows使用原Operator捆绑receiver.exe，限时18秒headless接收真实三路公网流，decode_ok=1；head/left约28.985fps，right29.495fps，三路进程全程存活，测试结束已清理。证据`.build/real-video-20261120/report.json`及各路日志。观察到head/right累计RTP缺10/6，left0；不代表长时间无丢包或UDP根因已解决。这里是实际解码验证，不是正式Operator窗口显示验收。

下一步等待两只真实USB Mate接入Windows并核对物理左右，再审查历史mapping/Home冲突；待机成功不等于已获运动启用。下文“尚未执行/待确认”是此次执行前的审查历史，不要再次运行一次性start_standby.py --apply；现有进程保持运行。若后续故障，可检查后显式执行`ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes yam196 "/home/ai/yam-teleop/bin/teleopctl retry-standby"`，该命令仍会启动真实相机，按现场权限处理。

## 最新方案：用户要求先启动YAM待机

本机既有State凭据已通过SZ_YAM02快照认证（HTTP200），证据`.build/state-auth-local-candidate-20261120.json`不含凭据。另有多份历史Home/Mate配置，但存在CAN左右互换、夹爪Home不同等冲突，不能当作当前设备已验收配置。原`to-teleop`已尝试，返回2（disabled），在停止评测或打开设备之前拒绝。下文旧表及旧流程的“未执行”“未找到认证”等以本节为准。

新增`to-standby`仅允许Agent与三相机sender；仍拒绝enable、server、bridge、handoff、robot、Home，即使commissioning以后满足也如此。保留enabled=false、commissioning全false、Home/mapping空。原完整to-teleop门禁不变；to-eval支持从standby恢复。

本地部署及SDK测试：74 passed、1 skipped（Windows不支持Linux fcntl锁），新增脚本AST检查通过；测试已修正为导入当前部署源码而非旧构建。远端Linux测试将在更新后、停止任何服务前执行，尚未运行。

完整待确认命令：

```powershell
python -B "E:\working\YAM\mate-yam-teleop\deployment\yam196\start_standby.py" --apply
```

该命令通过既有yam196 SSH的stdin传输5个小文件和内存凭据，不下载依赖，不在argv/报告/补丁中保存凭据。核对主机、配置hash、空闲和进程身份后，备份代码/manifest/station至`/home/ai/yam-teleop/backups/standby-20261120-9341e2fe64`，更新部署代码/测试及manifest、写0600凭据，只改state_enabled=true。远端部署+SDK测试失败即还原本次写入，不停止评测。

测试通过后，再复核trial空闲和源码hash/PID启动时间/完整参数，向父PID4147442发SIGTERM，等待父子PID4147442/4147446正常退出，最多45秒。已查源码：Uvicorn退出调用FastAPI lifespan，再Bridge.close通知worker shutdown并关闭stdin。不会强制kill，未释放就停止切换。trial API `127.0.0.1:18772/api/state`及原评测当前均idle、recording=false、preview=false；证据`.build/standby-trial-review.json`。

最后执行`/home/ai/yam-teleop/bin/teleopctl to-standby`，停止原空闲web/policy/gui，启动Agent/三路真实相机，等State同步和新编码帧，确认能力关闭、无会话/Home/ArmStack才成功。风险是中断原评测/trial入口、释放设备可能改变双臂保持状态，现场需有人看护；待机不发起跟随或Home。启动失败锁定fault、不自动重启评测。具体审查`.build/standby-review.json`。本次尚未上传、写凭据、退出trial、停服务或启动真实设备。

显式恢复：`ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes yam196 "/home/ai/yam-teleop/bin/teleopctl to-eval"`，也可能初始化评测设备，按现场状态确认。代码/配置回退需先停待机、确认设备释放，再使用上述备份，不可在运行中覆盖旧版本。

## 当前已核实

| 项目 | 观察结果 | 下一步 |
|---|---|---|
| Windows Mate | CIM及pyserial枚举仅COM3–COM6蓝牙串口，未见USB串口 | 接入两只Mate，确认物理左右与USB标识；不可把蓝牙COM当Mate |
| Operator候选 | `E:/working/YAM/YAM/arena-client/dist/YAM Operator-video-health/`内EXE、Python、receiver齐全 | 根据真实COM与认证配置生成独立现场profile，保留原配置 |
| State认证 | 候选指向`http://47.119.112.49:5052`，token为环境变量引用；Windows进程/用户/系统变量均未配置，.196的`config/state.token`不存在 | 确认既有token的安全存储位置与正确服务，不能依赖开发默认值；未连接或写入正式State |
| .196评测 | GUI/web仍PID1104813/1104912，policy active/exited；首次检查`prompt/recording=true`，最新复查已`idle/recording=false/preview=false` | 本轮未停止录制或评测；仍需核实trial退出和设备释放 |
| 独立设备进程 | PID4147442/4147446均为`.../trial-tools/ui_trial.py`，位于`session-563.scope` | 由当前试验入口正常退出并确认资源释放；不直接kill |
| CAN | `can_left`与`can_right`均up；原评测配置也是left/right同名对应 | 后续现场确认物理左右；接口up不证明方向、映射已正确 |
| 相机 | USB描述符存在D435一台、D405两台；评测配置SDK序列号与station一致 | 先释放原占用，再经确认逐路启动和核对画面；未打开RealSense pipeline |
| Mate映射 | 正式`server_mapping=null`；未找到这对Mate已验收的signs/scales/offsets | 提供已用配置来源或安排现场验证，不从示例直接宣称已标定 |
| Home | 正式左右Home均null；原评测配置无Home/start_joints目标 | 确认左右七维目标及坐标含义，不能填写猜测值或默认全零 |
| Teleop安装 | .196安装存在、服务disabled/inactive，全部commissioning=false | 参数与现场验证完成后才生成并审查启用配置 |
| Relay | UDP5004/5005 active，PID721/722，二进制与此前相同 | 暂不处理公网缺包根因；联调期间仍如实记录通信质量 |

相机USB/sysfs描述符中的serial不等于RealSense SDK serial，不能根据两个号码不同修改station。三路当前SDK配置为head263122074809、left260322278322、right260422273031，仍需实际画面核对物理角色。

## 已完成的软件验证及边界

既有59项State/Agent桥/Operator会话与UI逻辑/本机dry YAM+双bridge测试通过，证据`.build/e2e-preflight-20261120-7e9f29/results.xml`和`pytest.log`。

新增`verify_state_session.py`以独立随机robot、独立SQLite、loopback真实HTTP/WebSocket运行现有State、ArenaAgent硬件dry-run、Operator StateClient/StateAdapter/StateSync。能力确认、启动、Agent就绪、client-ready、active、正常停止/模拟Home、回到IDLE通过；活动时Owner/Mate锁持有、结束后Mate锁释放，服务线程退出。证据`.build/state-session-539bc7dd9bca/report.json`。

这不代表正式公网State、.196实际Agent/ArmStack、Mate、相机解码或双臂已通过。离屏源码窗口只作控件构建检查，截屏有字体方框，未据此验收实际Operator EXE界面或视频显示。用户选择真机准备后不继续扩展模拟演示。

## 设备启用顺序

1. 接入两只Mate并核对左右；取得State token的既有存储位置、已验收映射和Home配置来源。只提供位置，不在聊天中发送token值。
2. 当前评测正常收尾、保存录制并退出独立trial。复查`idle + recording=false + preview=false`及设备进程，无活动试验时才安排切换。
3. 先本地生成独立Windows profile和.196候选station差异，保留原值、标明每个参数来源及验证状态。token不写入报告、Git或命令行。远端文件写入另行审查确认；不把commissioning全置true冒充验收。
4. 首次相机/映射/设备释放验证若需单独维护步骤，先列完整命令和风险获得确认。原评测退出后逐路确认相机，机械臂初始化和Mate初始化独立确认，不在串口枚举时顺带执行解锁/多圈清零。
5. 所有正式门禁满足后，以现有控制器切入Teleop：启动Agent/视频并等待State同步和新帧，然后Operator打开目标SZ_YAM02，确认当前画面，启动会话，最后才允许Mate跟随。首次跟随前检查可触及的停止入口和运动空间。
6. 正常停止会话可能触发Home运动；Home必须单独明确确认。异常停止按既有abort路径处理，不能以“停止”名义隐含归位。
7. 会话结束且无Home/ArmStack活动，检查设备释放后，才通过控制器恢复原评测服务。

## 已有命令与执行状态

以下只读预检已经执行，返回2是当前正确拒绝：`disabled`及两个独立设备进程。`hardware_probed=false`。

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes yam196 "/home/ai/yam-teleop/bin/teleopctl preflight"
```

以下是后续切换/恢复的完整现有命令，**尚未执行，且当前条件不满足，不应直接运行**：

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes yam196 "/home/ai/yam-teleop/bin/teleopctl to-teleop"
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes yam196 "/home/ai/yam-teleop/bin/teleopctl to-eval"
```

`to-teleop`目标是.196，会停止原评测web/policy/gui并启动Agent和三路真实相机；会话启动再按Agent路径初始化双臂。主要风险为打断试验/录制、设备争用和后续机械臂运动。当前门禁会提前拒绝，不可为通过而绕过。失败锁定fault，不会自动重启评测。

`to-eval`会先检查无活动遥操/Home、关闭Teleop并确认释放，再恢复切换前记录的评测服务；恢复评测也可能初始化设备，须按现场状态确认。不能把它当作任何时候都可执行的无害回滚。

最终真实执行前，按用户AGENTS.md“任何可能影响真实世界设备、人员、安全或生产环境的命令，执行前必须获得用户明确确认”，再次展示将实际执行的命令、动作和风险。此处只是预备命令审查，不是已获得启用授权。

最新只读快照见`.build/real-teleop-readiness-20261120.json`：评测已idle且录制/预览关闭；两个独立trial PID仍在，preflight仍拒绝；Windows仍仅见COM3–COM6蓝牙串口，station SHA256未变。
