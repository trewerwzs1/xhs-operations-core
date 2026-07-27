# 三种推广输入方式

## 读取账号帖子

```text
读取我账号排在最前的有效帖子，根据它寻找相关用户，先只生成策略预览。
```

系统使用 `account_note`。先在本人主页的有界可见批次运行公共
`engage latest-account-note`；程序忽略置顶顺序，按唯一最新发布时间选择，缺失或
并列时间失败关闭。

## 指定参考帖子

```text
把我提供的这篇周末手作活动帖子作为本轮参考，根据这类内容寻找相关用户。
```

系统使用 `specified_note`。指定内容不得被账号真实最新帖子替换。

## 直接描述人群

```text
我想寻找对本地周末手作活动和城市生活体验感兴趣的用户。
```

系统使用 `direct_brief`，不要求存在帖子。

## 离线预览

```powershell
.\scripts\xhs-ops.ps1 engage strategy-preview `
  --project-root . `
  --account-id <AccountId> `
  --file work/strategy_manifest.json
```

`work/strategy_manifest.json` 必须由本次真实输入生成；示例文件只用于理解结构。预览必须返回 `platform_actions_executed=0`。用户可在策略执行前补充关键词或排除词，例如“可以找本地生活用户，但不要找商业广告账号”。

后续真实 Campaign 必须绑定该输出的 `source_ref`、`source_hash`、`strategy_id` 和 `content_hash`。不得使用示例中的 fixture ID、占位哈希或固定日期。
