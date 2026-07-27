# Codex Plugin 与兼容 Skill 使用说明

> 本文适用于 `2.0.0a0` 随包 Codex Plugin；独立 Skill 仅作为 V1 兼容入口保留。

项目随包提供 `plugins/xhs-operations-core` Plugin。Plugin 只公开 `setup`、`publish`、`service`、`engage`、`review` 五组工作流，并把自然语言任务映射到已测试的项目 CLI 和统一预检，不在运行时重新编写浏览器脚本。`skills/xhs-operations-core` 是 Plugin 读取的详细操作合同，也是升级兼容入口。

## 安装与发现

推荐运行 `scripts/install.ps1`。安装器会通过 Codex 官方 Plugin CLI 注册项目内 Marketplace 和 Plugin，并将兼容 Skill 复制到：

```text
%USERPROFILE%\.codex\skills\xhs-operations-core
```

安装和离线 UAT 通过后，重启 Codex Desktop，重新打开已解压且不会再移动的项目目录，再使用：

```text
$operate-xhs-operations-core 根据我的活动信息完成 Setup；需要人工加载扩展、扫码或精确确认真实写入时再通知我。
```

安装后的 CLI 命令统一通过 `.\scripts\xhs-ops.ps1` 调用项目 `.venv`，不要改用系统 Python。

## V2 2.0.0a0 能力状态

- Campaign、三类推广输入、Discovery、Candidate、MessagePlan、DailyPlan、heartbeat、每日复盘、最新帖子读取和账号 Style Setup 已进入 release 交付路径。
- 公开点赞、一级评论、精确回复和评论单独点赞只允许走固定 Run Agent，并受 STOP、审批、节流、去重和写后验证约束。
- 交付验收依据程序测试、23 项零平台动作离线 UAT、干净安装和 release checkout；交付前不要求绑定接收方真实账号。
- 真实账号登录、扩展加载和账号资料学习属于接收方本地 Setup。
- 主动私信只支持一个已持久化候选的一条精确审批消息；无精确候选、对端哈希、会话快照、审批与可见回读时保持本地记录。

首次安装后 STOP 与平台访问均关闭。必须完成 Setup、创建并确认真实 Campaign、授权精确任务和通过当前页硬门，才能执行公开写入。

## 多日运行条件

定时任务只负责唤醒 Codex。运行期间电脑必须开机且未休眠，Codex Desktop 必须运行，项目目录必须保持原路径，专用 Chrome profile 必须保持可用且已登录，Bridge 必须运行。电脑或应用重启后先恢复 Chrome 和 Bridge，并重新检查连接；错过的心跳不补跑。
