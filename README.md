# YAM .196 Remote Teleop

机器人：`SZ_YAM02`，目标主机：`ai@192.168.1.196`。保存远程 Teleop 的部署脚本、现场已部署源码及脱敏配置。

## 目录

- `deployed/`：从 .196 `/home/ai/yam-teleop/current` 只读取得的部署源码，包含 Agent、机械臂桥接、视频发送器和部署适配器。
- `station/config/`：现场配置快照；认证字段脱敏，`state.token` 不进入仓库。
- `station/systemd/`：三份实际用户级 systemd 服务定义。
- `station/snapshot-manifest.json`：采集时间、来源和快照文件校验和。
- `deployment/yam196/`：安装、修复、诊断、测试和 Windows 支持脚本；构建缓存与安装依赖不入库。

## 使用与恢复

此仓库保存配置与源代码，不会自动启用硬件。现场配置快照可能含已验收参数，应先核对当前设备、CAN 名称、相机序列号和原评测进程。不要直接覆盖正在运行的配置。

原安装器与构建器见 `deployment/yam196/README.md`。准备脚本默认使用历史 Windows 源工作区 `E:/working/YAM/YAM`；跨机器使用时需提供对应来源。`deployed/` 可直接审查当前部署源码，不依赖该 Windows 路径。

安装所需的 i2rt、RealSense、私有 Python 环境、离线 wheel 和编译依赖继续使用现场原环境；未将环境或大型二进制复制进 Git。本次未重新执行安装/恢复，也未验证从空白主机部署。

认证令牌应在目标主机本地配置 `/home/ai/yam-teleop/config/state.token`，权限 `0600`；Windows 继续使用原 `OPERATOR_STATE_TOKEN` 方式。令牌、SSH 密钥和密码均不入库。

## 正常使用与临时测试

正常双 Mate 使用既有控制入口。`mirror_*`、`open_operator_mirror_test.py`、`prepare_mirror_test.py` 是单 Mate 双臂的独立临时测试工具；并非正常模式选项。保留用于复现先前联调，不改变正常控制路径。

## 本地验证（不连接设备）

有现成 Python 测试依赖时，运行：

```powershell
python -B -m pytest deployed/tests -q
```

执行任何 start/stop、部署覆盖、机械臂动作或相机启用前，应先人工审查现场状态和完整命令。
