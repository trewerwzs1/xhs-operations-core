# XHS Operations Core V2 Roadmap

状态更新时间：2026-07-27

当前权威：`V2_CHECKOUT.json`

## 产品范围

V2 是一个单账号、低频、可恢复的通用小红书运营核心。Codex 负责理解用户
目标并编译任务，包内程序负责确定性执行。公开产品只有：

- `setup`：安装、连接、账号绑定与可选 AccountVoice；
- `publish`：按完整素材准备并发布一条内容；
- `service`：逐条处理可见入站评论或私信；
- `engage`：基于账号帖子、指定帖子或直接 brief 寻找相关内容并低频互动；
- `review`：只读查询或导出 OperationLedger。

## 自主执行收口

| 任务 | 状态 | 证据 |
|---|---|---|
| AUTO-CLOSE-01 | done | TaskIntent、ExecutionMandate、PolicyDecision、一次性 ActionPermit 与 exactly-once |
| AUTO-CLOSE-02 | done | 五组公共流程自主化；公开流程无逐动作授权 |
| AUTO-CLOSE-03 | done | 一次搜索、同批候选、单次帖子点赞、可见回读与 Ledger 已真实核验 |
| AUTO-CLOSE-04 | in progress | 重建唯一 ZIP，并用同一 SHA-256 完成 2/2 空目录安装 |

用户下达任务本身构成该任务的有界执行委托。普通流程不要求用户复制 hash、
批准 StrategyPack/Campaign、批准草稿或逐动作放行。每个 exact 动作仍必须
通过账号、目标、事实、风险、节流、去重、STOP、unknown/no-retry、单写
lease、可见回读和 Ledger 硬门；不合格动作自动 skip 或 stop。

## 当前真实能力

- Engage 自主帖子点赞核心链：`live_verified`；
- 固定 Run Agent、Bridge、专用 Chrome、账号身份和一次搜索/同批顺序处理：
  `live_verified`；
- Service 评论/DM inbox 读取：既有 `live_verified_empty_batch`，仅证明空批次
  读取，不冒充 item reply；
- 顶层评论、评论点赞与回复：保留历史 live 证据；自主公共链为
  `program_pass`，按任务和目标条件运行；
- Publish 图片/视频、Service 单项回复、主动单条 DM：公共合同和 Gateway
  为 `program_pass`，真实执行取决于接收方素材、可见 item 或稳定 peer ID；
- AccountVoice 可选；样本不足时使用中性语气，不阻断 `platform_ready`。

## 唯一真实调用链

所有真实小红书登录、读取、搜索、打开和写入只能通过：

`XhsOperationGateway -> ranfang_run_agent -> XHS Bridge -> dedicated Chrome`

Codex Browser、通用 Chrome 控制、Computer Use、直接 CDP、XHR、直接 URL 或
临时脚本均不得作为真实平台降级路径。

## 交付退出条件

- 当前源码全量回归和静态硬门通过；
- 最终包不包含 Git、浏览器 profile、登录态、runtime receipt、开发机路径、
  内部研发记录或 donor 依赖；
- 同一个最终 ZIP SHA-256 在两个全新目录安装和 offline UAT 均通过；
- 接收方只需解压、安装、重启 Codex、加载扩展、扫码登录、绑定账号并下达
  任务，不需要重新开发；
- `known_blockers=[]` 后才允许 `release_ready=true`。
