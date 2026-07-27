# XHS Operations Core V2 精简产品章程

状态：V2-CONVERGE-00 contract confirmed
日期：2026-07-22

## 1. 北极星

V2 是一个可由 Codex Desktop 安装、Setup、运行和恢复的单账号小红书通用运营核心。它把稳定的平台能力做成固定程序和 Skill，业务差异通过 `StrategyPack` 注入，不要求接收方临时开发。

第一目标是四条业务工作流真实跑通，并以只读记录能力提供恢复和审计事实：

1. `setup`：安装、Bridge、专用 Chrome、扫码登录、账号绑定，以及可选账号语气学习；
2. `publish`：图片笔记和视频笔记发布；
3. `service`：读取并回复评论和私信；
4. `engage`：围绕策略逐个阅读、判断、点赞、评论、回复或单条私信；
5. `OperationLedger`：只读保存和查询动作结果、异常、恢复点与证据；公开兼容入口为 `review status/list/export`。

## 2. 固定产品边界

- 一个本地 Workspace 默认只绑定一个小红书账号。
- 公共核心不绑定行业；活动 `Campaign` 是主动互动的一类策略包。
- Codex 是推理、规划和异常接管层；确定性程序负责合同、状态、权限、节流、去重、执行与证据。
- `AccountVoice` 只约束表达风格。`platform_ready` 与 `voice_ready` 分离；缺少本人回复样本时允许中性语气草稿，但文本写入必须逐条通过内部事实与语气审核，失败即跳过。
- 真实小红书读写只允许 `ranfang_run_agent -> XHS Bridge`。
- 写操作必须串行并经过统一 preflight；节奏由动作类型与用户配置共同决定。
- 不声称任何节奏可以规避平台控制。
- 不迁移燃放项目的业务人设、事实、话术、账号数据或知识库。

## 3. V2 用户闭环

```text
安装与 Doctor
-> Bridge/专用 Chrome/账号 Setup
-> 本人历史语料形成 AccountVoice
-> 选择 publish/service/engage 任务
-> StrategyPack 与目标确认
-> 每次一个精确平台动作
-> OperationReceipt 与恢复点
-> OperationLedger 只读查询或导出
```

## 4. V2 不做

- 不使用 Codex 通用浏览器、官方 Chrome 控制或 Computer Use 作为真实小红书降级路径。
- 不做群发、批量轰炸、关注增长、多账号矩阵、店铺、直播、广告或交易。
- 不在缺少账号/页面绑定、必要事实、写入许可或可见回读时执行写入。
- 不让接收方为了 Setup 临时开发浏览器自动化。

## 5. 完成定义

V2 只有在 `V2-CONVERGE-00` 至 `V2-CONVERGE-06` 全部完成、四个业务工作流与只读 Records 均有相应程序及真实验收证据、同一 ZIP 两次首次空目录安装成功且已知 blocker 为空时，才可生成候选交付包。任何尚未真实验证的能力必须明确标记，不得用文档、菜单或 mock 代替跑通证据。
