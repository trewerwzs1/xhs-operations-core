# XHS Operations Core V2 精简架构

## 1. 总体分层

```text
Codex Plugin: setup | publish | service | engage | review
  -> Application: workflow + StrategyPack + AccountVoice
  -> XhsOperationGateway: one preflight + one command + one receipt
  -> ranfang_run_agent -> XHS Bridge -> dedicated Chrome
  -> Local State: journal + recovery + OperationLedger
```

## 2. 四个边界

### Codex 与应用层

Codex 负责理解目标、生成结构化草稿和选择下一条合法命令，不直接点击小红书。应用层只维护 `setup`、`publish`、`service`、`engage` 四种业务工作流；`review` 是 `OperationLedger` 的只读兼容查询入口。`AccountVoice` 保存账号语气特征，`StrategyPack` 保存业务目标与搜索/回复策略。

### 统一 Preflight

所有真实写入只经过一次公共检查：账号与页面绑定、事实与写入许可、节流与去重、审计与恢复。检查失败即停止；不再为每种业务堆叠重复 Gate。

运行模式是显式合同：

- `offline`：无论业务布尔门是否为真都禁止写入；
- `autonomous_task`：TaskIntent 已物化为有界 ExecutionMandate，内部 PolicyDecision
  只能为一个 exact plan 签发一个一次性 ActionPermit；动作预算在 transport
  前消费；
- `scoped_uat`：仅为旧数据与研发回归保留，不进入接收方公共流程；
- `recipient_release`：`V2_CHECKOUT.json` 与 vendor manifest 必须同时
  `execution_enabled/release_ready` 且 blocker 为空。

产品 checkout 是 V2 发布真相，vendor manifest 只是固定执行层真相，后者不能单独开启 Recipient Release。

### 平台 Gateway

平台能力必须注册并由单一 Gateway 调用。V2 浏览器提供者不是自由可切换的执行器：

| Provider | 允许用途 | 真实小红书登录/读/写 |
|---|---|---|
| XHS Bridge / Run Agent | 产品生产执行、精确回读、证据 | 唯一允许 |
| Codex built-in Browser | localhost UI、打包 DOM fixture、明确授权的公开只读研究 | 禁止 |
| Codex Chrome extension | 一般已登录网页协作实验 | 禁止作为产品依赖 |
| Computer Use | 非小红书桌面辅助测试 | 禁止 |

### 本地状态

每个动作产生稳定 ID、输入 hash、策略/许可引用、统一 `OperationReceipt`、transport journal 和恢复点。Publish、Service、Engage 的 verified/not_dispatched/unknown 终态都写入统一 result journal，随后由 `OperationLedger` 只读投影。unknown 永久标记 `do_not_retry`；人工对账只解除全局 STOP，不允许同一 dedupe key 自动重试。日志不得记录 Cookie、token、完整隐私语料或不必要的页面原文。

`OperationLedger` 只负责追加式事实记录和 `status/list/export` 查询，不产生行业判断、次日建议或自动调量。历史 DailyReview 可兼容读取，但不得进入公共发布门或核心工作流。

### Setup readiness

Setup 同时报告 `platform_ready`、`voice_ready` 与 `operations_ready`。`platform_ready` 只取决于安装、Bridge、扩展实例、登录、账号身份和本地配置；`voice_ready` 独立表示 post/reply 画像完整性。缺少本人回复样本时不得伪造或重复扫描已耗尽批次，文本草稿退化为中性语气并强制 `review_each`。

## 3. 为什么保留 XHS Bridge

Codex 内置浏览器使用独立浏览器 profile，不自动继承用户现有 Chrome 标签和登录态；通用 Chrome 控制虽然能使用既有登录态，但它不是 XhsOperationsCore 已封装的账号绑定、动作编译、全局节流、写入 journal 和回读合同。XHS Bridge 已经是产品协议的一部分，V2 应增强它的端口和测试，而不是绕开它。

## 4. 迁移方式

```text
public surface       # setup/publish/service/engage + review(records only)
existing V1 modules  # 通过薄适配层继续复用
platform/xhs         # 唯一 live adapter
skills + plugin      # Codex 的固定调用说明与入口
```

不进行一次性全量重写。内部旧 CLI 暂时保留用于兼容和测试；用户文档与最终 Plugin 只暴露五组入口。

搜索、页面导航、帖子点赞、一级评论、评论点赞、评论回复和单条私信复用固定 V1 vendor 原语。`platform/xhs/provenance.py` 固定 source release、archive/tree hash 与逐文件 hash；只有受 hash 漂移影响的 primitive seam 需要重验，不能据此重写未变化动作。
