# XHS Operations Core

> A local-first Xiaohongshu operations core for Codex Desktop.
>
> 面向 Codex Desktop 的本地优先、单账号小红书运营核心。

[![Release](https://img.shields.io/github/v/release/trewerwzs1/xhs-operations-core?include_prereleases&label=release)](https://github.com/trewerwzs1/xhs-operations-core/releases)
[![License](https://img.shields.io/github/license/trewerwzs1/xhs-operations-core)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Codex%20Desktop-5C2D91)](docs/user-guide/06_windows_install_and_first_run.md)
[![Status](https://img.shields.io/badge/status-public%20alpha-orange)](V2_CHECKOUT.json)

XHS Operations Core 把小红书账号运营中可重复、容易出错的部分固化为本地程序和
Codex Skill。Codex 负责理解目标和制定策略，包内程序负责确定性执行、节流、
去重、STOP、结果回读和操作记录。

它不是某个行业的运营方案，也不内置任何行业事实、话术或案例。业务目标、事实、
受众和表达策略通过外部 `StrategyPack` 注入，因此同一套核心可以服务不同的
单账号运营项目。

## 适合什么场景

- 内容发布：准备并发布图文或视频笔记。
- 评论与私信客服：读取账号收到的评论或私信，生成并执行单条回复。
- 主动互动：根据一篇帖子或一段业务描述生成受众与查询策略，逐个阅读候选并互动。
- 账号语气学习：从账号本人历史回复中提取表达特征；样本不足时退化为中性语气。
- 操作追踪：通过只读 `OperationLedger` 查询动作结果、异常和恢复点。

当前版本只面向**单账号、低频、本地运行、人机协同**。它不提供多账号矩阵、
批量群发、广告投放、交易、直播或规避平台控制的能力。

## 五组公开工作流

| 工作流 | 作用 | Alpha 证据状态 |
| --- | --- | --- |
| `setup` | 安装、配置、扩展连接、账号与语气初始化 | 程序与干净安装通过 |
| `publish` | 图文/视频发布任务 | 程序链通过，真实发布仍需用户素材与账号环境 |
| `service` | 评论/私信客服任务 | 程序链通过，真实空队列读取已验证 |
| `engage` | 策略、搜索、候选阅读与单动作互动 | 核心真实链已验证 |
| `review` | 只读查询和导出 OperationLedger | 程序链通过 |

详细、机器可读的当前状态以 [`V2_CHECKOUT.json`](V2_CHECKOUT.json) 为准。
Alpha 不把“程序存在”冒充为所有账号环境下的真实平台验收。

## 快速开始

### 1. 下载

从 [Releases](https://github.com/trewerwzs1/xhs-operations-core/releases)
下载最新 ZIP 和 `SHA256SUMS.txt`。当前 Alpha：

[`xhs-operations-core-2.0.0-alpha.0.zip`](https://github.com/trewerwzs1/xhs-operations-core/releases/download/v2.0.0-alpha.0/xhs-operations-core-2.0.0-alpha.0.zip)

```text
SHA-256
9a28c434d8cae7ee83803b966d2acbdd3a2f149b2b7902f1198f0bbb379b9c43
```

### 2. 在 Codex Desktop 中打开

1. 将 ZIP 解压到一个空目录。
2. 使用 Codex Desktop 打开解压后的项目根目录。
3. 让 Codex 完整读取并执行 [`HANDOFF_PROMPT.md`](HANDOFF_PROMPT.md)。
4. 按引导加载随包 XHS Bridge 扩展，并在专用 Chrome 中扫码登录小红书。
5. 运行离线验收，确认安装和固定能力可用后再创建真实任务。

接收方不需要 Git、不需要 donor 项目，也不需要重新开发浏览器自动化。

### 3. 手动安装方式

如果需要直接使用 PowerShell：

```powershell
.\scripts\install.ps1 -AccountId my_xhs_account -ProfileName my_xhs_profile
.\scripts\offline-uat.ps1
```

所有产品命令统一通过：

```powershell
.\scripts\xhs-ops.ps1 --help
```

## 运行方式

```text
用户任务
  -> Codex Desktop
  -> setup / publish / service / engage
  -> TaskIntent + ExecutionMandate
  -> 内部 PolicyDecision + 单次 ActionPermit
  -> XhsOperationGateway
  -> ranfang_run_agent
  -> XHS Bridge
  -> 专用 Chrome
  -> 可见结果回读
  -> OperationLedger
```

用户下达的任务本身构成该任务的有界执行委托。普通产品流程不会反复要求用户
复制 plan hash 或逐动作授权口令；账号、目标、事实、风险、节流、去重、STOP、
unknown/no-retry、lease 和可见回读仍由内部硬门约束。

## 安全与平台边界

- 真实小红书访问只允许固定链：
  `XhsOperationGateway -> ranfang_run_agent -> XHS Bridge`。
- 不把 Cookie、密码、验证码、浏览器 profile 或私密客户数据写入仓库。
- 风险提示、账号不一致、目标漂移、未知写结果或 STOP 状态均失败关闭。
- 未知写结果不会自动重试。
- 账号写动作全局至少间隔 600 秒。
- 不绕过验证码，不提供批量骚扰、未成年人定向或平台风控规避能力。

本项目是独立、非官方的本地工具，不隶属于或获得小红书官方背书。使用者应遵守
平台规则、适用法律、内容权利和所在组织的政策。详见
[`SECURITY.md`](SECURITY.md) 与
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 文档导航

- [干净电脑安装与首次运行](docs/user-guide/09_clean_computer_handoff.md)
- [Windows 安装](docs/user-guide/06_windows_install_and_first_run.md)
- [XHS Bridge 首次连接](docs/user-guide/08_xhs_bridge_first_connection.md)
- [能力矩阵](docs/user-guide/10_capability_matrix.md)
- [V2 产品章程](docs/v2/00_product_charter.md)
- [V2 架构](docs/v2/01_architecture.md)
- [自主执行架构](docs/v2/11_autonomous_execution_architecture.md)
- [公开发布与推广素材](docs/PUBLIC_LAUNCH.md)

## 参与项目

这是公开 Alpha。最有价值的反馈不是“再加更多功能”，而是：

- 在全新 Windows 电脑上的安装结果；
- 扩展连接、扫码登录和恢复路径是否清晰；
- 某个公开工作流在哪一步失败；
- 日志、状态和错误信息是否足以定位问题；
- 文档中是否存在不真实或容易误解的能力声明。

提交问题前请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)。安全问题不要包含凭据、
Cookie、私密对话或可工作的攻击细节。

## License

[MIT](LICENSE) © 2026 XHS Operations Core contributors
