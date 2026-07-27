# AccountVoice Setup 合同

## 目标

让接收方扫码登录自己的单个小红书账号后，通过固定入口读取有限、可见、可证明属于本人的历史帖子和历史回复，分别形成 `post_voice` 与 `reply_voice`。Codex 后续只把画像作为表达约束，不能让风格覆盖事实和写入规则。

## 唯一工作流

```text
setup/account-config
-> setup extension-enroll / connection-check / account-enroll
-> setup enable-platform-read
-> setup voice-learn (可按 next_note_position 增量重复)
-> setup voice-status
-> setup status
```

`setup voice-learn` 直接适配既有内部历史学习实现，真实读取仍只有 `ranfang_run_agent -> XHS Bridge`。同一批本人主页快照同时生成两类画像，不重复扫描：

读取前的可见前提由用户完成：在专用 Chrome 的本人主页中，将第一排帖子卡片滚动到视窗内。程序只处理稳定 ID、当前可见且通过 pointer hit-test 的卡片；如果没有符合项，失败关闭并提示用户定位页面，不自动循环滚动。

- `post_voice`：只保存标题/正文长度、段落、标签、emoji、问号、叹号和列表密度等去事实化特征；不保存历史帖子标题或正文；
- `reply_voice`：保留用户同意的本地安全回复 corpus，并生成不含回复原文的聚合 profile。

## Voice 完成条件

- `post_voice` envelope、profile content hash、账号和历史 snapshot 绑定均可验证；
- `post_voice.stores_raw_post_text=false` 且至少有一篇安全本人帖子；
- 本地 reply corpus envelope 可验证且账号一致；
- 至少两条无隐私标记的本人回复形成 profile；
- profile envelope 可验证、不保存原文，并与当前 corpus 的 sample ID、reply hash 和 snapshot hash 一致；
- `voice-status.ready=true`；
- `setup status.voice_ready=true`，且 `setup status.steps.account_voice=true`。

缺少样本返回 `post_voice_required`、`reply_voice_required` 或 `continue_required`；完整性或绑定异常返回 `invalid` 并失败关闭。昵称相同不构成本人证据，隐私样本不会进入 corpus/profile，CLI 不返回整批历史帖子或回复原文。

`voice-learn.learning.has_more` 是是否继续当前本人主页批次的唯一游标。只有 `has_more=true` 才返回 `next_step=continue_history_capture`。若它为 `false` 且状态仍为 `reply_voice_required` / `continue_required`，说明当前账号没有足够的真实本人回复，返回 `next_step=use_neutral_review_each`；不得对同一耗尽批次循环读取，也不得用昵称、外部评论或生成文本伪造 reply_voice。

此时保留已通过的 `post_voice`，并明确输出：

- `voice_ready=false`，原因是 `insufficient_owned_reply_samples`；
- `drafting_policy.mode=neutral_review_each`、`confidence=low`；
- `text_write_approval_mode=review_each`、`review_each_required=true`；
- 允许生成中性草稿，但每条文本写入仍必须经过现有 exact approval 和统一 Preflight。

## 与 Setup 的关系

AccountVoice 只约束表达风格，不授予事实或写入权限。完整样本不足不阻断 `platform_ready`；安装、账号配置、Bridge/扩展连接和登录校准完成时，`platform_ready=true` 与 `voice_ready=false` 可以同时成立。`operations_ready` 只表示可以进入当前本地草稿/审批前置流程，不等于真实写入已启用；`live_write_ready` 仍为 `false`。

若 corpus/profile envelope、账号绑定或样本 hash 无效，状态为 `invalid`，`operations_ready=false`。这种完整性漂移不得退化到中性草稿，必须先修复或重新学习。

## 手动边界

Chrome 扩展加载和扫码必须由接收方在可见界面完成。自动化测试验证合同、固定调用路径、增量合并、隐私过滤和恢复；它不能伪造某个接收账号已经扫码或授权。
