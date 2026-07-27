# XHS Operations Core

> V2 Stage 1–5 的程序路径已完成，Stage 6 的 Codex Plugin 已通过官方 CLI 隔离安装验证，正在生成并验证两份干净安装。公开入口固定为 `setup`、`publish`、`service`、`engage`、`review`。V2 当前真相只看 `V2_CHECKOUT.json` 和 `docs/v2/`。

> Python 包版本现为 `2.0.0a0`。这是一份 V2 alpha 候选，不表示真实平台 UAT 已完成；真实账号读取与写入状态仍必须以 `V2_CHECKOUT.json` 的 pending 项为准。

`XHS Operations Core V2` 的目标是可复用的单账号小红书运营核心：Codex 负责语义与编排，包内程序和 Skill 固定 Setup、发布、客服、主动互动与复盘能力，行业策略通过 StrategyPack 注入。

核心不捆绑行业知识。Codex 根据用户的真实输入生成通用
`StrategyPack`，程序只校验来源、事实、受众、查询、动作范围、节流和恢复边界。

## 运行时

产品通过 **Codex Desktop** 运行：

- Codex Desktop 负责理解活动、制定计划、审批编排和调用项目 CLI。
- 小红书真实访问只允许使用项目内固定版本的燃放 Run Agent（XHS Bridge）；Playwright、Computer Use、直接 Chrome 和临时脚本全部冻结。
- 本项目代码负责确定性的 Campaign、配置、策略输入、风控、节流、去重、状态和审计能力。
- 用户不需要重新开发代码，但首次安装需要加载随包 XHS Bridge 扩展并登录自己的小红书账号。
- 每台新电脑或每个新 XhsOperationsCore Chrome profile 需要一次“加载已解压的扩展”确认；详见 `docs/user-guide/08_xhs_bridge_first_connection.md`。

## 核心能力

- 最新活动帖子只读识别、Campaign 与授权事实。
- 由任意业务输入生成的受众画像和分层查询计划。
- 单候选阅读、证据评分、帖子短评和定向回复计划。
- 首次激活时读取账号历史帖子与本人回复，生成不保存原文的聚合风格画像。
- 单目标审批、STOP、登录、风险、节流、去重、结果验证和 ActionRecord。
- 单帖子互动包：一级评论最多1条、合格回复1–3条、剩余意向评论点赞或记录、特别相关对象进入私信候选。
- DailyPlan、原子单心跳队列、失败恢复和每日复盘。
- 私信合同、评论者主页/会话导航、被动回复规划、主动外联单消息审批、精确可见回读和隐私保护。

当前实现包括：当前页流式 InteractionSession、健康 Tab 绑定、风险读取、Unicode 门、普通搜索页归一化、同批逐候选、三个可信编译器、四个原子单写分支、逐动作 ActionRecord、账号级写入租约与 journal、未知结果人工核对、任务绑定授权、调度 occurrence 幂等、分类日上限、风格档案完整性、意向用户/私信状态持久化和本地每日指标复盘。最终测试数字和未通过门只以 `RELEASE_CHECKOUT.json` 及最终验收报告为准。

真实平台动作默认关闭。安装、离线预览和 Style Setup 都不会自动授权写入。

## Windows 安装

在 PowerShell 中运行：

```powershell
.\scripts\install.ps1 -AccountId my_xhs_account -ProfileName my_xhs_profile
```

安装器创建项目本地 `.venv`、安装 Codex Skill、生成本地配置与独立浏览器 profile，并保持 STOP 和平台访问禁用。详见 `docs/user-guide/06_windows_install_and_first_run.md`。

安装完成后运行零平台动作的离线验收：

```powershell
.\scripts\offline-uat.ps1
```

验收会覆盖活动分析、风格学习、回复计划、单目标队列、每日复盘、私信与审批桥，并断言没有启动浏览器、没有执行任何平台动作。

空 Codex 接收 ZIP 后应从 `HANDOFF_PROMPT.md` 开始，并以 `docs/user-guide/09_clean_computer_handoff.md` 为完整主流程。首个可运行任务必须按以下状态链建立：

```text
真实输入 -> 策略预览 -> Campaign create -> ready/active
-> engage start -> autonomous heartbeat/status/stop
```

所有安装后产品 CLI 命令统一通过 `.\scripts\xhs-ops.ps1` 调用项目 `.venv`。真实任务不得使用 fixture ID、占位哈希、示例事实或固定日期。

多日运行期间，电脑必须开机且未休眠，Codex Desktop、专用 Chrome 和 Bridge 必须运行，已解压项目目录必须保持原路径。重启后先恢复 Chrome、Bridge 和连接检查；错过的心跳不补跑。

## 目录约定

```text
src/xhs_operations_core/    Python 产品代码
tests/                 自动化测试
docs/migration/        从旧仓库抽取公共能力的记录
config/                可提交的示例配置
skills/                随项目交付的 Codex Skill 源码
vendor/                固定版本的 Run Agent 与第三方许可证
scripts/               Windows 安装、运行和干净交付构建脚本
```

本地账号、浏览器 profile、登录态、密钥和运行数据不得提交到版本库。

## 项目关系与合规

本项目是独立的非官方本地工具，不隶属于或获得小红书官方背书。使用者应遵守
平台规则、适用法律及账号所属组织的政策。项目默认单账号、低频运行，不提供
批量群发、账号矩阵、规避平台控制或绕过验证码的能力。详见 `SECURITY.md` 和
`THIRD_PARTY_NOTICES.md`。
