# Windows 安装与首次运行

> 本文适用于 `2.0.0a0` V2 交付候选包。

## 前置条件

- Windows 10/11
- Codex Desktop
- Python 3.11 或更高版本（`python` 命令或 Windows `py` launcher 任一可用）
- 可访问 Python 包索引的网络连接（首次安装依赖时需要）
- 已安装 Chrome

## 1. 安装

在项目目录打开 PowerShell：

```powershell
.\scripts\install.ps1 `
  -AccountId my_xhs_account `
  -ProfileName my_xhs_profile
```

默认行为：

- 创建 `.venv` 并安装 XHS Operations Core 及固定 Run Agent 依赖。
- 安装 `xhs-operations-core` 到个人 Codex Skill 目录。
- 生成 `config/project.local.json` 和 `config/browser.local.json`。
- 全量刷新安全收口后的 XHS Bridge 到 `%LOCALAPPDATA%\XhsOperationsCore\xhs-bridge-extension`，并逐文件校验 SHA-256；账号登录态仍只保存在用户自己的 XhsOperationsCore Chrome profile 中。
- 创建 runtime/log/report 目录。
- 写入 STOP，保持 `allow_platform_access=false`、`writes_allowed=false`。

安装不会访问或写入小红书。

## 2. 验证

```powershell
.\scripts\xhs-ops.ps1 doctor --project-root . --init-runtime --format json
```

必须看到 `ok=true`。

随后运行完整离线验收：

```powershell
.\scripts\offline-uat.ps1
```

必须看到：

- `ok=true`
- `check_count` 与 `RELEASE_CHECKOUT.json` 的 `offline_uat_expected_check_count` 一致
- `stop_enabled=true`
- `platform_access_allowed=false`
- `platform_actions_executed=0`
- `browser_started=false`

这一步只使用合成 fixture，不启动浏览器，也不访问小红书。

## 3. 重启 Codex Desktop

重启后在项目任务中调用：

```text
$xhs-operations-core 根据我的活动信息运行离线预览，不要访问小红书。
```

## 4. XHS Bridge 首次连接

新电脑或新 Chrome profile 需要一次浏览器确认。完整步骤、检查命令、换电脑和升级处理见 [08_xhs_bridge_first_connection.md](08_xhs_bridge_first_connection.md)。这不是重新开发；安装器准备全部文件，用户只批准加载未上架扩展。

## 5. 首次账号流程

1. 按向导加载安装器已复制到 `%LOCALAPPDATA%` 的 XHS Bridge，并要求 `connection-check` 中 Bridge、Extension、暂存版本和登录检查准备状态均为 true。
2. 启动本地 Bridge，人工登录并运行 Run Agent 登录检查。
3. 只读 Style Setup：读取有限数量的账号历史帖子、评论和本人回复。
4. 生成不保存原文的 ReplyStyleProfile。
5. 用户可通过公共 `engage latest-account-note` 读取当前账号可见主页批次中发布时间最新的有效帖子，或直接指定帖子/输入目标描述，创建 StrategyPack 与 Campaign。
6. 人工确认活动事实、目标城市、活动周期和允许动作。
7. 按 `docs/user-guide/09_clean_computer_handoff.md` 创建 task JSON，并用 `engage start -> heartbeat/status/stop` 运行自主任务。
8. 运行 Discovery、Candidate、Message、DailyPlan 和 Review。

真实 Campaign 和任务文件必须由本次输入与 CLI 输出生成。不得授权含 fixture ID、占位哈希、示例事实或固定示例日期的文件。

旧 `browser login-check/login-authorize/profile-probe/latest-readonly/candidate-*` 命令已经冻结，调用会在打开浏览器前失败。不得把这些历史命令当成备用链路。

## 6. 首个 Campaign 与真实写入硬门

交付前不要求真实账号验收。真实账号只在接收方本地 SET UP 时扫码登录；安装、账号配置、扩展/Bridge 连接和登录校准达到 `platform_ready` 后，首个 StrategyPack 的搜索和互动就是正式使用流程，不是补开发或补交付验收。AccountVoice 是独立的可选增强；`voice_ready=false` 时文本草稿只能使用低置信度中性语气，并逐条通过内部事实与语气审核，失败即跳过，不向用户追加逐动作授权。

真实写入必须同时满足：

- 本地 STOP 存在时，只允许精确计划、短时收据覆盖的单目标动作；不得全局删除 STOP 绕过审批。
- Run Agent 供应商 manifest 明确 `execution_enabled=true` 且 `known_blockers=[]`。
- XHS Bridge 已连接且登录检查有效。
- 无验证码、异常登录、频繁操作等风险信号。
- 目标帖子、评论或 DM 对端精确匹配；主动 DM 只允许已记录候选的一条精确审批消息。
- 正文 hash、Campaign、Candidate、审批记录全部匹配。
- 每日预算和至少 600 秒目标间隔通过。
- 用户已确认 Campaign 的运行范围；公开文本仍绑定精确计划和批准记录。

任一条件不满足即停止，不尝试绕过平台控制。

## 7. 多日运行条件

- 电脑保持开机且不休眠，Codex Desktop 保持运行。
- 已解压项目目录保持注册定时任务时的原路径，不移动、不改名、不改用临时 worktree。
- 专用 Chrome profile 保持可用且已登录，Bridge 保持运行。
- 电脑或应用重启后，从原项目根目录重新启动 Chrome 和 Bridge，并通过公共 `setup connection-check`。
- 错过的心跳跳过，不补跑、不集中追量。

## 8. 构建干净交付目录

在项目外指定一个不存在的输出目录：

```powershell
python .\scripts\build_delivery.py `
  --output C:\Temp\xhs-operations-core-delivery `
  --archive C:\Temp\xhs-operations-core-delivery.zip
```

构建器会验证必须文件存在，并拒绝携带本地配置、浏览器 profile、登录态、runtime、日志和报告。ZIP 内只有一个顶层项目目录，结果会同时输出文件大小和 SHA-256 校验值。
