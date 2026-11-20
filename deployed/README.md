# SZ_YAM02 独立 Teleop 安装

目标：`ai@192.168.1.196` / `frodobot`。本包只安装，默认禁用。现有评测代码、环境、标定及服务不变。

## 制作与安装

1. `python capture_baseline.py`：经授权只读目标，记录驱动/模型与评测文件 SHA256、服务状态及已安装依赖版本。
2. `python build_bundle.py`：从 `E:/working/YAM/YAM` 复制最小运行模块，在本次生成的副本中适配；原工作区不修改。
3. `python -m pytest tests .build/release/piper-mate/Python_SDK/test_yam_remote_dual_server.py -q`：模拟硬件及本机回环。
4. 经单独批准后运行 `prepare_deps.py` 和 `prepare_deps.py --json-only`：固定依赖、来源和校验和。
5. `python deploy.py`：仅生成依赖压缩包、完整远端脚本及审查清单，不联网、不安装。
6. 审查 `.build/install-review.json`、`.build/remote-install.py` 与 `install.py`，明确批准后才运行 `python deploy.py --apply`。

安装位置 `/home/ai/yam-teleop`。安装器只对新用户服务执行 `daemon-reload`，不执行 start/stop/enable。Python 安装仅使用 uv 离线缓存及已下载文件；若缓存不全则报错，不自动联网。编译为单任务、nice 15、空闲 I/O 优先级。RealSense 禁用联网检查、更新、DDS、图形工具及 ROSBAG2；使用系统现有 libusb 和私有 JSON 3.12.0。

首次安装拒绝覆盖已有目录/同名服务。失败时保留本次生成的目录和诊断，不能把它视为安装成功。只有 `logs/install-result.json`、`logs/offline-validation.json` 都通过才交付。

## 配置与启用限制

唯一站点参数入口是 `/home/ai/yam-teleop/config/station.json`。相机型号、SDK 序列号、会话名、分辨率、帧率、码率均在其中。主视角 D435：263122074809；左 D405：260322278322；右 D405：260422273031。不要替换成 USB/ASIC 编号。

`enabled=false`、`state_enabled=false`、全部 commissioning=false、`server_mapping=null`、`home=null` 是本次交付的预期值。真实入口在创建机器人/相机或连接公网前拒绝启动。原始 sender 二进制也有独立启动检查。`--dry-run` 只允许模拟 Server；Agent 的 dry-run 不能绕过网络启用检查。

后续现场验收须明确填写两侧 Mate 的 signs/scales/offsets/跟随与对齐速度（采用现有 server 的逗号分隔字段）、Home 七维目标，并验证三相机、设备释放和公网通信。认证 token 单独存放在 `config/state.token`，权限 0600。不要只翻转布尔值冒充验收。现有软件硬超时仍允许重新基准化；`--no-auto-resume` 不等于所有掉线都永久锁定。零力矩软件模式也不等于电机断电。

## 操作与恢复

`/home/ai/yam-teleop/bin/teleopctl status` 和 `preflight` 不初始化设备。未验收时 preflight 返回非零且说明禁用原因是正常结果。

`to-teleop` 与 `to-eval` 是后续经确认后使用的设备相关命令。控制器检查当前评测 idle、录制和预览关闭，拒绝独立 trial、遗留进程和不完整检查。保存原运行状态后按 web→policy→gui 停止评测，再启动三路视频和 Agent；视频有新帧且 State 同步后才打开 Teleop 能力。机械臂由正式会话启动。

切换失败锁定 fault，不自动重启评测。`to-eval` 拒绝活动会话/Home，确认释放后只恢复原来运行的服务。并发操作有文件锁，但不能约束手工绕过工具的其他 SocketCAN 程序；这类入口应先退出或纳管。普通切换不隐含 Home。

## 回滚

本轮服务保持停用，故回滚不需要设备动作：核对清单和哈希后移除仅本次新建的三个 unit 文件，执行用户 `daemon-reload`，保留版本目录及安装日志供恢复。已有评测文件无需恢复。若服务已被后续启用，必须先取得设备停机确认并检查释放，再回滚；不要直接删除运行中的版本。

本次无 prior current 指针可回退；未来版本保留 releases 和 prior pointer，再在确认无活动进程后切换指针。删除源代码、现场标定、评测环境、录制数据、共享缓存都不属于本包回滚。
