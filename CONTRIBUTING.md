# Contributing

感谢你帮助改进 XHS Operations Core。

当前阶段是公开 Alpha，优先接受能够提高安装成功率、真实工作流稳定性、错误可诊断性
和文档准确性的贡献。请避免在没有证据的情况下扩大产品边界。

## 提交 Issue 前

请先确认：

1. 使用的是最新公开 Alpha；
2. 已阅读 `HANDOFF_PROMPT.md` 和对应用户指南；
3. 已运行 `.\scripts\offline-uat.ps1`；
4. 问题不包含密码、Cookie、验证码、浏览器 profile、私信原文或客户隐私数据；
5. 同一问题尚未被现有 Issue 覆盖。

建议提供：

- Windows 与 Python 版本；
- Codex Desktop 版本；
- 使用的公开工作流：`setup`、`publish`、`service`、`engage` 或 `review`；
- 预期结果与实际结果；
- 已脱敏的错误码、命令输出和最小复现步骤；
- 是否发生任何真实平台读取、搜索或写入。

## Pull Request

1. 每个 PR 只解决一个清晰问题；
2. 不引入新的真实小红书浏览器调用路径；
3. 不提交账号、登录态、Cookie、密钥、运行日志或业务客户数据；
4. 新能力必须有可调用接口、自动化测试和真实/占位/待联调证据等级；
5. 文档不得把 fixture、mock 或程序测试描述成真实平台通过；
6. 运行相关聚焦测试和 `.\scripts\offline-uat.ps1`；
7. 在 PR 中说明改了什么、为什么改、如何验证，以及是否触发平台动作。

真实小红书访问仍只允许：

```text
XhsOperationGateway -> ranfang_run_agent -> XHS Bridge
```

不要添加 Playwright、Computer Use、直接 Chrome 或临时脚本作为真实平台降级路径。

## 安全问题

请遵循 `SECURITY.md`。不要在公开 Issue 中披露凭据、私密内容或可工作的攻击细节。
