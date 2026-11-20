# 单 Mate 控制真实双 YAM：独立测试入口

准备完成，尚未部署新测试代码、尚未打开 Mate 串口、尚未使能真实双臂。正常 Operator 源码和原 profiles 未修改。本次功能只通过独立脚本显式启动。

## 目标与参数

COM18 一次 UART 读取，在同一把锁下取快照，复制给左右独立序号的命令块。复制的是关节数据，不是笛卡尔空间镜像。读失败或尚无首个有效样本时两路均 stop。

使用既有 `E:/working/YAM/YAM/piper-mate/Python_SDK/yam_server_config.yaml` 的逐字节副本 `yam_deploy/mirror_normal_server.yaml`。对齐速率仍为 2,2,2,2,2,2，跟随仍为 8,8,6,8,8,8 rad/s；signs/scales 全 1，offsets 全 0，gripper_scale 12.5、范围 0..0.75。未新增限速或角度限制。仅改变服务 loopback 地址/端口、强制双臂成功和独立调试日志目录。

这份正常参数来源并不证明该 Mate 已完成当前现场标定；本次是经现场确认的试动，不把 station.enabled/commissioning/Home 伪造为已验收。

## 已完成检查

本地原部署、SDK 和测试适配检查 97 passed，1 skipped（Linux fcntl 在 Windows 跳过）；之后新增部署成功顺序/测试失败文件精确还原检查 2 passed。远端 Linux 新版本检查将在明确确认后、切换 Agent 前运行，尚未执行。

只读 SSH 快照 `.build/windows-mirror-test/standby-snapshot.json`：mode standby，Agent State connected/synced=true，teleop_enabled=false，无 session/Home/ArmStack；三路真实视频和 Agent active，原三评测服务 inactive，无独立设备占用。station SHA256 为 `1a263ea1f62e3104a18f32675169fe8ed49441a1ce9e4f488a4b12f0a0b8ffaf`。

## 待用户明确确认的完整操作

第一步部署独立门禁并启用测试能力：

```powershell
python -B "E:\working\YAM\mate-yam-teleop\deployment\yam196\prepare_mirror_test.py" --apply
```

它通过现有 yam196 SSH 在 `/home/ai/yam-teleop/current` 备份并更新五个明确列出的文件及 manifest：launch.py、runtime.py、mirror_test.py、正常参数副本、test_mirror_runtime.py。写前重新比较现场配置/源码哈希并检查待机、空闲与无占用。备份位于 `/home/ai/yam-teleop/backups/mirror-test-YYYYmmdd-HHMMSS/`。运行远端硬件隔离测试，失败则精确还原文件，保持原 Agent；测试通过才重启 Agent、切到 mirror-test 并启用能力。保留三路视频和 previous_eval，不恢复原评测。部署后启用失败锁定 fault、停 Agent，不自动重启评测。

第二步打开独立真实测试窗口：

```powershell
& "D:\Users\Administrator\AppData\Local\Programs\Python\Python310\pythonw.exe" -B "E:\working\YAM\mate-yam-teleop\deployment\yam196\open_operator_mirror_test.py" --mirror-test --mate COM18 --launch
```

窗口显示 ONE MATE / TWO REAL ARMS TEST 和双臂运动横幅；只使用真实 State/Relay/三相机，禁止退回 Relay 控制。凭据仅使用已有环境变量或此前核实的本机位置，在进程环境中传递，不写入 profile、命令参数或报告。此命令打开窗口，不自动点击 Teleop，不自动打开 Mate。

用户点击该窗口 Teleop 启动时，现有 Agent 将初始化 can_left/can_right 两只真实 YAM；Windows 子进程的完整命令由正常配置路径解析：

```powershell
& "E:\working\YAM\YAM\arena-client\dist\YAM Operator-video-health\runtime\python\python.exe" "E:\working\YAM\mate-yam-teleop\deployment\yam196\mirror_mate_test.py" --mirror-test --mate COM18 --server-ip 127.0.0.1 --server-port 5011 --rate 60.0
```

该子进程构造现有 PiperMateReader，会对 Mate 发送现有 `Stop_On_Control_Mode(..., "unlocked", 900)` 和 `reset_multi_turn_angle(0xFF)`，随后一份输入同时驱动两只真实臂。双臂可能先按原参数对齐到 Mate 的绝对关节角，再跟随；两只臂的运动空间、碰撞和停止/释放后支撑状态需要现场看护。这里不是只读串口检查。没有 Home 目标，不执行 Home。

## 停止与恢复

测试窗口的 Stop 保留 Windows 本地栈退出核查，但 State 请求改用已有 teleop_abort，无 Home；窗口关闭和掉线沿用已有 abort。测试模式在进程入口继续拒绝 Home（即使其他窗口发出 Home 请求也不能打开 Home 设备）。先用 Stop，确认 State/Agent 无 session/Home/ArmStack 后才退出测试能力：

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes yam196 "/home/ai/yam-teleop/bin/teleopctl mirror-test-stop"
```

这条命令关闭能力并重启原待机 Agent，保留相机和原评测快照，不恢复评测、不运行 Home。若仍有会话/ArmStack，拒绝切换。正常模式的 to-teleop 仍要求全部真实 commissioning/Home 条件。

恢复旧代码：只在测试会话结束、Agent 与设备所有者完全退出并经确认后，依据上述备份精确还原更新文件/manifest；备份前不存在的新文件仅在同一已确认回退中删除。不要在真实会话运行期间覆盖文件。恢复评测仍使用原独立确认流程，不能把 to-eval 当作本次无害退出。

确认来源是用户提供 AGENTS.md：“任何可能影响真实世界设备、人员、安全或生产环境的命令，执行前必须获得用户明确确认。确认时说明目标、完整命令、预期动作和主要风险。”此前“允许”覆盖的是 standby 操作，本审查的实际测试部署/双臂运动命令是新操作。
