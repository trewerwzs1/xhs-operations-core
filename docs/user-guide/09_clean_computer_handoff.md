# 空白电脑交接

接收方只需要一个交付 ZIP、Codex Desktop、Chrome、网络和 Python 3.11+。
不需要 Git、donor 项目或现场开发浏览器自动化。

## 安装

```powershell
.\scripts\install.ps1 -AccountId <安全别名> -ProfileName <安全别名>
.\scripts\offline-uat.ps1
```

安装后重启 Codex Desktop 并重新打开项目。

## Setup

启动包内 Bridge 和专用 Chrome。用户只负责可见地加载一次扩展、扫码登录，
以及必要时处理验证码。保存账号配置后检查状态：

```powershell
.\scripts\xhs-ops.ps1 setup configure --project-root . --file work/account_setup.json
.\scripts\xhs-ops.ps1 setup status --project-root . --account-id <account-id>
```

AccountVoice 可选；历史本人回复不足时使用中性语气，不阻断
`platform_ready`。

## 下达任务

从 `examples/autonomous/` 复制最接近的模板到 `work/`，由 Codex 根据用户原始
要求填写。task JSON 不包含调用者提供的 hash 或确认字段。

```powershell
.\scripts\xhs-ops.ps1 engage start --project-root . --task-file work/engage-task.json
.\scripts\xhs-ops.ps1 engage heartbeat --project-root .
```

Service 使用 `service start/heartbeat/status/stop`；Publish 使用
`publish prepare/run/status`。任务一经下达，内部策略、计划、policy 和 permit
由系统自动推进，不再逐步向用户索要业务授权。

所有真实平台访问只能走固定 Run Agent/Bridge/专用 Chrome。每个 heartbeat
最多一个动作，账号写入至少间隔 600 秒；一次搜索、同批候选顺序处理；
unknown 不重试。

交接完成的判断以 `V2_CHECKOUT.json`、offline UAT、相同 ZIP 的两次干净安装
和真实核心链证据为准。
