# XHS Operations Core 项目规则

## 项目目标

把本项目开发成可交付给实际使用者、能在其本地环境安装和运行的通用小红书运营核心。Codex 是大脑，包内确定性程序与 Skill 提供固定能力。

## 产品边界

- 产品形态是 `setup`、`publish`、`service`、`engage` 四个业务工作流，加只读 `OperationLedger` 基础能力。
- 为兼容五组公开命令名，`review` 只保留 `status`、`list`、`export`，不得承载通用业务复盘、线索质量判断、策略建议或自动调量。
- `Campaign` 是主动互动的一类 `StrategyPack`，不是公共平台层的唯一核心对象。
- 行业策略与验收案例只能作为外部 `StrategyPack` 数据提供，公共平台层不得内置任何行业事实。
- V2 只做单账号、低频、可恢复的人机协同；不扩展多账号、店铺、直播、广告或交易能力。
- `AccountVoice` 是可选表达约束，不提供业务事实或写入许可；缺少本人历史回复不得阻断 `platform_ready`，文本写入必须退化为中性语气并逐条通过内部事实与语气审核，失败即跳过。
- 不复用 `ranfangAI` 中减肥塑身业务的具体人设、事实、话术、数据、案例和知识库。
- 可以抽取其通用架构与经过测试验证的公共代码。

## 工程规则

- **V2 开发状态**：`codex/v2-foundation` 分支上的 V2 当前真相由根目录 `V2_CHECKOUT.json`、`docs/v2/11_autonomous_execution_architecture.md`、`docs/v2/12_autonomous_closure_execution_plan.md` 和 `docs/v2/` 决定。`docs/v2/09_architecture_convergence_execution_plan.md` 与 `docs/v2/10_final_closure_plan.md` 保留历史收敛证据。V1 的 `RELEASE_CHECKOUT.json` 只表示继承基线，不能证明 V2 可发布或可执行。

- **V2 授权边界**：用户下达任务本身构成该任务的有界执行委托。普通产品流程不得再次要求用户复制确认口令、plan hash 或 exact action 授权。StrategyPack 与 Campaign 是业务快照；每个 exact ActionPlan 由内部 PolicyDecision 根据 ExecutionMandate 自动生成一次性 ActionPermit。账号、目标、事实、风险、节流、去重、STOP、unknown、lease、可见回读和 Ledger 硬门继续保留。超范围动作跳过，风险动作停止，不以向用户索要放行作为恢复路径。
- **V2 浏览器边界**：Codex 内置 Browser、官方 Chrome 控制和 Computer Use 只能用于 localhost/本地 DOM 验证或明确授权的公开只读研究，不得进入真实小红书登录、读取或写入调用链。真实小红书仍只允许 `XhsOperationGateway -> ranfang_run_agent -> XHS Bridge`；V2 可以重构端口和编排，但不得添加通用浏览器降级路径。

- **接收方部署模式**：当目录来自交付 ZIP、没有 `.git`，且用户目标是安装或运行时，优先读取 `HANDOFF_PROMPT.md` 和交付 Skill。接收方不需要访问任何开发机源码或 donor 项目，不需要执行迁移审计，也不得为了 SET UP 临时开发浏览器自动化；只运行包内安装、验收和固定 CLI。交付包会有意省略内部研发证据，目录缺失不是包损坏。以下“先审计旧项目”规则仅适用于产品源码开发模式。

- **小红书调用方式硬锁**：所有真实小红书登录、读取、搜索、打开帖子和写入动作，必须使用燃放项目已验证的 `ranfang_run_agent` 调用方式。现有 Playwright、直接 Chrome、通用浏览器扩展控制、Computer Use、临时脚本和任何其他调用方式全部冻结，不得作为降级或应急替代。包内固定且已审计的 XHS Bridge 是 `ranfang_run_agent` 的组成部分，不属于被冻结的通用扩展控制。Run Agent 适配与验收完成前，真实平台能力必须失败关闭，只允许离线开发和本地 DOM 测试。

- **先审计、后设计、再开发**：任何新功能动手前，先检查本项目 `docs/migration/`、包内固定 `vendor/xiaohongshu-skills/` 快照及现有测试证据。历史 donor 仅在开发机存在且当前证据不足时作为可选补充；其缺失不得成为开发、测试、安装或交付依赖。审计结论要写入本项目 `docs/migration/` 或任务审核记录，明确“直接迁移、适配迁移、仅借鉴、禁止复用”四类决定。
- 可迁移范围仅限通用平台能力、浏览器执行层、状态机、风控、节流、去重、审计、测试框架和调度架构；不得迁移减肥/塑形业务的人设、事实、话术、数据、案例、策略规则或知识库。
- 如果旧项目已有真实验证通过的通用方案，不得在未说明差异的情况下重新临时发明一套；确需重写时，必须记录旧方案不适用的具体证据。
- 公共平台能力、通用运营合同和 StrategyPack 必须分层。
- 所有真实平台动作必须经过权限、事实、风控、节流、去重和审计硬门。
- 每个模块必须有可调用接口和自动化测试。
- 真实能力、占位能力和待联调能力必须明确标注。
- 不写死本机绝对路径、账号、登录态、Cookie、密钥或用户隐私数据。
- 默认先离线测试，再只读 preview，最后按单动作 smoke 逐步开放真实写入。
- 开发遵循 `docs/development/00_north_star_prompt.md` 和 `docs/development/01_self_loop_protocol.md`。
- 每次只推进一个已定义任务；实现、测试和严格审核全部通过后，才能把下一任务设为进行中。
- 测试失败、审核存在阻断问题或能力仍为占位时，不得将任务标记完成。

## 文档路径

- 产品与技术架构：`docs/architecture/`
- 迁移与抽取记录：`docs/migration/`
- 调研与验证报告：`docs/research/`
- 用户安装与运行文档：`docs/user-guide/`
- 开发北极星、任务队列和验收记录：`docs/development/`
- V2 产品章程、架构、Roadmap 和 Setup：`docs/v2/`
