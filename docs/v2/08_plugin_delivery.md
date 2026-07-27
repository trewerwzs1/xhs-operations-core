# V2 Codex Plugin 与交付合同

## 公开产品边界

接收方只看到五组能力：`setup`、`publish`、`service`、`engage`、`review`。Plugin 是 Codex 的入口层，不实现第二套小红书浏览器能力；详细合同、确定性 CLI 和真实动作仍在项目包内。

## 固定资产

- Marketplace：`.agents/plugins/marketplace.json`
- Plugin：`plugins/xhs-operations-core/.codex-plugin/plugin.json`
- Plugin Skill：`plugins/xhs-operations-core/skills/operate-xhs-operations-core`
- 安装器：`scripts/install-plugin.ps1`，只调用 Codex 官方 `plugin marketplace` / `plugin add` / `plugin list` 命令
- 总安装入口：`scripts/install.ps1`

安装器支持空白 Codex 配置、同目录重装和项目目录变化后的 Marketplace 重新绑定。安装后必须回读 Plugin 的 installed、enabled 和精确版本；任一不匹配即失败关闭。

## 接收方步骤

1. 解压 ZIP 并定位唯一项目根目录。
2. 运行 `scripts/install.ps1` 与 `scripts/offline-uat.ps1`。
3. 重启 Codex Desktop并重新打开项目。
4. 用 `$operate-xhs-operations-core` 从 `setup status` 的 `next_step` 继续。
5. 用户在可见 Chrome 中加载随包 XHS Bridge，并扫码登录。

接收方不访问 Git、donor 项目或开发机路径，也不临时开发浏览器自动化。

## 验收合同

同一个精确 ZIP 必须在两个不同的空安装目录、两个隔离的 USERPROFILE/HOME/CODEX_HOME/LOCALAPPDATA 中分别通过：

- 正常 `install.ps1`；
- Codex Plugin installed/enabled/version 回读；
- Plugin、兼容 Skill、XHS Bridge 与包内树哈希一致；
- 46 项 STOP-on 离线 UAT；
- Extension/Bridge DOM fixture 与包内扩展树绑定；
- `platform_actions_executed=0`；
- `git_dependency=false`、`donor_dependency=false`。

真实平台 UAT 与程序/交付验收分开记录。Plugin 或两次干净安装通过，不能替代账号本人历史读取、图文/视频发布、客服读写及五种 Engage 写入的真实验收。
