# 自主任务 CLI

用户只需提供一次完整任务。Codex 生成一个项目内 task JSON，不要求用户复制
hash 或逐步批准内部对象。

Engage 示例：

```powershell
Copy-Item examples\autonomous\engage_task.json work\engage-task.json
.\scripts\xhs-ops.ps1 engage start --project-root . --task-file work/engage-task.json
.\scripts\xhs-ops.ps1 engage heartbeat --project-root .
.\scripts\xhs-ops.ps1 engage status --project-root .
```

三类输入分别使用 `account_note`、`specified_note`、`direct_brief`。
`specified_note` 必须保留用户指定内容，不能换成测试账号的真实最新帖子。

task JSON 只含 instruction、source mode/ref、requested actions、duration 和
allowed fact IDs。程序内部创建 StrategyPack/Campaign 快照、ExecutionMandate、
exact plan 和一次性 permit。

一个 heartbeat 最多执行一个动作；候选不合格时回同一搜索批次继续。超范围、
事实不足、重复、间隔未到或日上限耗尽自动跳过；风险、账号/目标漂移、STOP 或
unresolved unknown 自动暂停。
