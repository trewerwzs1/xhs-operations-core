# XHS Operations Core V2 自主执行总架构

状态：目标架构已确认；AUTO-CLOSE-01 核心授权合同程序通过，公共工作流迁移进入 AUTO-CLOSE-02

确认时间：2026-07-27

负责人：Codex（项目负责人、总架构师、执行与验收负责人）

## 1. 本次架构纠偏

现有 V2 把以下不同概念都实现成了“用户批准”：

- StrategyPack 确认；
- Campaign 确认；
- CampaignTask 授权；
- 草稿或编译结果批准；
- Scoped UAT 只读授权；
- exact action 写入授权；
- bounded write receipt；
- 单动作 lease。

其中只有任务目标与允许范围属于产品意图；其余大部分是程序内部的一致性、安全与并发控制。把所有内部控制暴露给用户，造成了重复确认、计划 hash 漂移后重新确认、真实 UAT 被人为切碎以及“测试通过但产品没有跑通”的问题。

新架构作出以下根本调整：

1. 用户下达任务就是执行委托，不再另设业务授权步骤。
2. Codex 负责把自然语言任务编译成一次不可变、可审计的 `ExecutionMandate`。
3. 每个具体平台动作仍然是 exact 单动作，但其执行许可由程序根据 `ExecutionMandate` 自动生成。
4. 所有 hash、permit、receipt、lease 和恢复信息只在内部流转，不要求用户复制或确认。
5. 目标、事实或风险无法安全确定时，动作直接跳过或任务停止，不向用户索要“放行授权”。
6. 扫码登录、验证码、扩展加载等客观的人机界面动作仍可能需要用户实际操作；它们不是业务授权。

## 2. 产品目标与边界

产品继续保持五组公开能力：

```text
setup
publish
service
engage
review
```

其中：

- `setup` 完成安装、Bridge、专用 Chrome、账号绑定和可选 AccountVoice 学习；
- `publish` 根据明确的发布任务完成一次图片或视频发布；
- `service` 根据客服任务逐项读取并处理评论或私信；
- `engage` 根据运营任务完成策略、一次搜索、逐个候选和低频互动；
- `review` 只读取和导出 OperationLedger，不负责业务策略。

V2 继续只支持单账号、低频、可恢复的人机协同，不增加多账号矩阵、批量群发、直播、广告、交易或 CRM。

## 3. 新的权威对象

### 3.1 TaskIntent

`TaskIntent` 是用户自然语言任务的不可变来源记录，例如：

- “根据我最新的帖子，连续 5 天寻找相关用户互动”；
- “按照这篇周末手作活动帖子寻找本地活动和城市生活兴趣人群”；
- “发布这组图片和正文”；
- “启动评论客服，在每天 10:00–20:00 逐项回复”。

任务中的动作动词决定允许范围：

- “寻找、分析、预览”只允许读取；
- “互动”默认允许点赞、顶层评论和评论回复；
- “私信”只有任务明确包含私信时才允许主动私信；
- “发布”只有任务明确要求发布并提供完整素材时才允许发布。

模糊任务采用保守默认值，不请求额外批准：

- 可以读取、搜索和生成策略；
- 可以执行低风险、任务范围内的互动；
- 不自动扩张到主动私信或发布；
- 缺少必要事实时跳过相关文本，不虚构事实。

### 3.2 ExecutionMandate

Codex 将 `TaskIntent` 确定性编译为一个 `ExecutionMandate`。它是整个任务唯一的执行边界，至少包含：

- account_id；
- workflow；
- source mode、source ref 和 source hash；
- StrategyPack 与 Campaign 引用；
- 开始/结束时间、每日时间窗和时区；
- allowed_actions；
- 每日上限、目标上限和至少 600 秒账号级间隔；
- 允许事实、缺失事实和禁止声明；
- 人群、查询词、排除词和一次搜索约束；
- 主动私信与发布是否被任务明确要求；
- AccountVoice 模式；
- 风险和停止规则；
- mandate hash。

`ExecutionMandate` 由本地程序根据 TaskIntent、Setup 合同和系统安全上限生成。它不需要第二次用户确认。

任务内容发生实质变化时生成新的 mandate 版本，不静默修改旧版本。

### 3.3 ActionPlan

每次 heartbeat 最多生成一个 `ActionPlan`：

- 一个账号；
- 一个 workflow；
- 一个 exact action kind；
- 一个 exact note/comment/peer/conversation；
- 一个完整正文或明确无正文；
- 一个 target/context/content/plan hash；
- 一个可见回读方法；
- `max_actions=1`。

禁止把点赞、评论、回复或私信组合成一次写入。

### 3.4 PolicyDecision

`PolicyDecision` 是系统内部的自动判定，替代所有面向用户的 exact approval：

```text
ActionPlan
-> 是否属于 ExecutionMandate
-> 是否满足事实与文本规则
-> 是否匹配当前账号、页面和目标
-> 是否满足时间窗、上限、间隔与去重
-> 是否没有风险、STOP 或未对账 unknown
-> allow / skip / stop
```

- `allow`：自动生成内部 `ActionPermit`；
- `skip`：记录原因并继续下一个候选；
- `stop`：停止任务并记录客观 blocker。

PolicyDecision 不向用户提出批准问题。

### 3.5 ActionPermit

`ActionPermit` 是一次性内部执行票据：

- 由 PolicyDecision 自动签发；
- 精确绑定 mandate、plan、target、content 和 account；
- 只允许一个动作；
- 有很短的有效期；
- 消费一次后失效；
- 不暴露为用户命令或确认词。

它替代现有用户侧 `MessageApproval`、`PublishApproval`、`ServiceReplyApproval`、`DMSingleApproval` 和 exact UAT approval 的交互职责。旧数据结构可在迁移期作为兼容适配器保留，但不再出现在公开流程中。

### 3.6 Lease、Receipt 与 Ledger

- `lease` 只解决并发与 exactly-once，不代表业务授权；
- `receipt` 只证明某次读取或写入执行边界，不代表用户批准；
- `OperationLedger` 记录 `verified / not_dispatched / unknown` 和恢复信息；
- 所有对象均为程序内部证据，不要求用户复制 hash。

## 4. 新的总调用链

```text
User task
  -> Codex reasoning plane
  -> TaskIntent
  -> ExecutionMandate
  -> Public Workflow API
       setup | publish | service | engage | review
  -> Strategy / Drafting / Scheduler
  -> exact ActionPlan
  -> PolicyEngine
       facts | scope | account | target | risk
       pacing | caps | dedupe | STOP | recovery
  -> internal ActionPermit
  -> account-global single-write lease
  -> XhsOperationGateway
  -> pinned ranfang_run_agent
  -> packaged XHS Bridge
  -> dedicated Chrome
  -> visible platform readback
  -> OperationReceipt
  -> OperationLedger
```

真实小红书仍只有以下平台调用路径：

```text
XhsOperationGateway
-> ranfang_run_agent
-> XHS Bridge
-> dedicated Chrome
-> Xiaohongshu
```

不得增加 Playwright、通用 Chrome 控制、Computer Use、直接 CDP 或临时脚本作为真实平台降级路径。

## 5. 四条自主工作流

### 5.1 Setup

```text
install
-> offline UAT
-> dedicated Chrome / Bridge
-> extension connection
-> QR login
-> account enrollment
-> platform_ready
-> optional AccountVoice learning
```

Setup 只处理环境和账号，不创建临时平台读写授权。账号已绑定且平台健康时，读取能力由当前 TaskIntent/ExecutionMandate 自动获得。

### 5.2 Engage

```text
task input
-> TaskIntent
-> StrategyPack
-> Campaign snapshot
-> ExecutionMandate
-> one exact query / one search / one saved batch
-> open one candidate
-> read and decide
-> exact ActionPlan
-> automatic PolicyDecision
-> one action
-> visible readback / Ledger
-> return to same batch
```

不再存在以下用户步骤：

- strategy-confirm；
- campaign-confirm；
- task-authorize；
- action approve；
- exact hash authorization。

StrategyPack 和 Campaign 继续保存业务事实与策略，但不再充当用户批准状态机。

### 5.3 Service

```text
service task
-> bounded inbox scan
-> one exact item
-> context read
-> reply draft
-> automatic PolicyDecision
-> one reply
-> visible readback / Ledger
```

退订、未成年人、隐私、敏感或目标不确定时直接跳过或停止，不向用户请求放行。

### 5.4 Publish

```text
explicit publish task + complete media/content
-> immutable publish plan
-> automatic PolicyDecision
-> one publish action
-> visible terminal readback / Ledger
```

“生成草稿”不等于“发布”；只有 TaskIntent 明确包含发布动作时才生成 publish mandate。缺少素材或正文时任务标记 `blocked_missing_input`，不是等待授权。

## 6. 自主恢复规则

### 6.1 可以自动处理

- 候选不相关：记录并继续同批次下一候选；
- 当前动作超出 mandate：跳过；
- 间隔未到：等待下一个合法 heartbeat；
- 页面未进入目标但写入尚未派发：安全返回并重新读取；
- 写入结果 unknown：冻结 exact target，执行只读对账；
- 对账为 verified：记录成功并继续；
- 对账为 verified_absent：记录失败、隔离 exact target，在 STOP 清理后继续新的合法目标；
- 任务到期、超上限或窗口外：no-op。

### 6.2 必须停止但不索要授权

- CAPTCHA 或平台风控；
- 登录失效；
- 账号身份漂移；
- Bridge/扩展 build 或实例不匹配；
- 无法对账的 unknown；
- 明确平台写入限制；
- 程序、vendor 或交付 hash 漂移。

这些情况生成客观 blocker。只有确实需要扫码、验证码或重新加载扩展时，才说明所需的人机操作；不得包装成“请批准继续”。

## 7. 公共命令目标

公开入口最终收敛为：

```text
setup doctor/configure/status/voice-learn/voice-status
publish prepare/run/status
service start/heartbeat/status/stop
engage start/heartbeat/status/stop
review status/list/export
```

兼容期可以保留旧 `preview/confirm/approve/authorize` 内部命令和数据读取，但：

- 不出现在普通用户 help；
- 不由 Skill 要求用户调用；
- 不接受用户粘贴 hash 作为正常产品流程；
- 只用于旧数据迁移、开发回归或兼容适配。

## 8. 必须保留的安全不变量

取消用户逐动作授权不等于取消安全边界。以下不变量保持：

1. 每个 heartbeat 最多一个平台写入。
2. 每账号所有写入至少间隔 600 秒。
3. 同一账号同时最多一个写 lease。
4. exact account/page/target/context/content 全部匹配。
5. 不虚构时间、价格、名额、行程、场地、主办方、报名方式或其他业务事实。
6. 不向未成年人推广酒类，不生成医疗或保健功效声明。
7. 不批量私信，不从昵称猜测身份。
8. opt-out、风险信号、账号漂移和 CAPTCHA 直接 STOP。
9. unknown 不重试同一 exact target。
10. 所有真实调用只经过固定 Run Agent 路径。

## 9. 状态模型

任务状态：

```text
created
-> running
-> paused_blocked | completed | cancelled
```

不再存在 `awaiting_user_confirmation` 或 `awaiting_exact_write_authorization`。

动作状态：

```text
prepared
-> permitted | skipped | stopped
-> dispatched
-> verified | not_dispatched | unknown
-> reconciled
```

`permitted` 由程序自动生成，不是用户批准。

## 10. 迁移原则

本次不推倒重写平台层：

- 保留 StrategyPack、Campaign、Task、compiler、Preflight、lease、Gateway、Run Agent 和 Ledger；
- 将 StrategyPack/Campaign 从“批准状态机”降为不可变业务快照；
- 将 `CampaignRunAuthorization` 收敛为 `ExecutionMandate`；
- 将各 workflow 的 approval 转成统一内部 `ActionPermit`；
- 将 `approval_ready` 改为 `permit_ready`；
- 将 Scoped UAT exact 用户授权改为项目负责人生成的 `ClosureMandate`；
- 通过适配器读取旧 approval/authorization 记录，禁止新增用户侧依赖；
- 逐步从 public help、Skill、handoff 和用户指南移除确认词。

## 11. 完成定义

只有以下条件同时满足，新架构才算完成：

- 普通用户流程中不存在必须复制的确认口令或 hash；
- 一条用户任务可以直接形成 ExecutionMandate 并自主运行；
- 所有写入仍通过统一 PolicyDecision、permit、lease、Gateway 和 Ledger；
- 风险、去重、间隔、目标绑定和 unknown 负例继续通过；
- Engage 主流程完成一次真实可见写入并记录 Ledger；
- 当前源码 ZIP 在两个空目录完成 2/2 安装；
- Skill、公共 help、文档、代码和 Checkout 使用同一架构词汇。
