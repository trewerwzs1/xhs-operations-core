# XHS Bridge 首次连接

## 固定边界

真实小红书只允许：

```text
XhsOperationGateway -> ranfang_run_agent -> XHS Bridge -> XhsOperationsCore 专用 Chrome
```

不得使用通用 Chrome 控制、Computer Use、Codex Browser、Playwright、直接
CDP、XHR/fetch、直接 URL 或临时脚本替代。

## 首次加载

1. 运行交付包的 Bridge/Chrome 启动脚本。
2. 打开专用 Chrome 的 `chrome://extensions`，启用开发者模式。
3. 选择“加载已解压的扩展程序”，目录必须是安装器输出的 XHS Bridge
   目录，且该目录直接包含 `manifest.json`。
4. 在同一专用 Chrome 打开小红书并扫码登录。
5. 保存账号配置并检查状态：

   ```powershell
   .\scripts\xhs-ops.ps1 setup configure --project-root . --file work/account_setup.json
   .\scripts\xhs-ops.ps1 setup status --project-root . --account-id <account-id>
   ```

`setup status` 会自动、幂等地绑定当前扩展实例和当前可见平台账号。用户无需复制
实例 ID、账号 hash 或确认短语。

只有连接版本、扩展实例、账号和登录状态全部匹配时，`platform_ready=true`。
状态仍未就绪时，只按照 `human_action_required` 完成可见界面操作，不开发新的
浏览器路径。

## 常见故障

- 扩展列表没有出现：关闭专用 Chrome，确认所选目录直接含 `manifest.json`，
  再用包内脚本重开后加载一次。
- build 不匹配：在扩展页点击“重新加载”，再运行 `setup status`。
- instance 不匹配：确认当前窗口是安装器创建的专用 profile；不要把其他项目
  的 profile/Cookie 复制进来。
- 账号不匹配、验证码、社区风险或操作频繁：立即停止，不换工具绕过。
- 换电脑：重新安装、重新加载扩展并扫码；不需要修改代码。
