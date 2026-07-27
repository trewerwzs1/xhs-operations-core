# Public launch kit

这份文件用于公开 Alpha 的轻量发布和早期用户招募。目标不是追求大量曝光，而是找到
少量真实使用者，验证“下载、安装、连接、运行、恢复”是否成立。

## 一句话定位

中文：

> XHS Operations Core 是一个运行在 Codex Desktop 中的本地优先小红书运营核心，
> 把发布、客服、主动互动和操作记录固化为可安装、可恢复的单账号工作流。

English:

> XHS Operations Core is a local-first, single-account Xiaohongshu operations
> core for Codex Desktop, with fixed workflows for setup, publishing, service,
> engagement, and auditable recovery.

## GitHub / 技术社区发布文案

```text
XHS Operations Core 2.0.0 Alpha 已开源。

它不是一个行业运营模板，而是一套运行在 Codex Desktop 中的通用小红书运营核心：

- setup：安装、扩展连接与账号初始化
- publish：图文/视频发布任务
- service：评论与私信客服
- engage：策略、搜索、逐个候选阅读与低频互动
- review：只读 OperationLedger

核心特点：
- 本地优先、单账号、低频运行
- 行业策略通过 StrategyPack 注入
- 固定真实平台链，不做通用浏览器降级
- 内置节流、去重、STOP、unknown/no-retry 和可见结果回读
- 提供 Windows 安装脚本、Codex Skill、Plugin 和完整交接说明

当前是公开 Alpha，优先寻找愿意测试“干净电脑首次安装”和真实工作流恢复能力的用户。

Repository:
https://github.com/trewerwzs1/xhs-operations-core

Release:
https://github.com/trewerwzs1/xhs-operations-core/releases/tag/v2.0.0-alpha.0
```

## 小红书 / 朋友圈短文案

```text
最近把一套小红书运营能力整理成了可安装的通用核心。

它运行在 Codex Desktop 里，支持账号 setup、内容发布、评论/私信客服、主动互动和操作
记录。业务策略是外部注入的，所以不是只服务某一个行业。

现在公开的是 2.0.0 Alpha，重点不是追求功能数量，而是验证：
1. 新电脑能不能顺利安装；
2. 扩展和账号能不能稳定连接；
3. 任务中断以后能不能恢复；
4. 每一步是否有清晰记录。

如果你平时会用 Codex，也有自己的小红书账号运营场景，欢迎试用和反馈。

GitHub： https://github.com/trewerwzs1/xhs-operations-core
```

## 推荐推广顺序

### 第一阶段：5–10 名真实测试者

优先邀请：

- 已经使用 Codex Desktop 的开发者；
- 有单账号小红书运营需求的小团队；
- 愿意在全新 Windows 环境记录安装过程的人；
- 能提供脱敏错误信息，而不是只说“不能用”的测试者。

此阶段只看四个指标：

1. ZIP 下载与校验成功率；
2. 首次离线 UAT 通过率；
3. XHS Bridge 首次连接成功率；
4. 第一个真实任务是否完成或能给出可诊断 blocker。

### 第二阶段：公开内容演示

第一阶段稳定后，再制作一个 60–90 秒演示：

```text
下载 ZIP
-> Codex Desktop 打开
-> 读取 HANDOFF_PROMPT
-> 离线 UAT
-> 扩展连接
-> 创建一个无平台写入的策略任务
-> 查看 OperationLedger
```

演示中不要展示真实 Cookie、二维码、私信、客户数据或账号隐私。

### 第三阶段：社区分发

可以发布到：

- GitHub Topics 与 Release；
- 小红书 Codex / AI Agent / 自动化相关内容；
- 即刻、V2EX、掘金或开发者社群；
- Codex、Python、Windows 自动化相关交流群。

每次只强调一个价值点，例如“干净电脑可安装”“StrategyPack 行业解耦”或
“unknown write 不自动重试”，不要一次讲完全部架构。

## 暂时不建议做的事

- 不建议购买流量或大范围投放；
- 不建议承诺“全自动无人值守”；
- 不建议把程序测试写成所有真实平台能力都已验收；
- 不建议用真实客户账号做公开演示；
- 不建议在 Alpha 阶段同时扩展多账号、店铺、直播、广告和交易；
- 不建议为了曝光加入规避平台风控、批量触达或群发卖点。

## Alpha 成功标准

满足以下条件后，再考虑扩大推广：

- 至少 5 台非开发机完成安装；
- 至少 3 名外部用户完成第一个任务；
- 安装失败都有明确错误码或恢复路径；
- 没有凭据泄漏、重复写入或未知写自动重试；
- README、能力矩阵和实际行为一致。
