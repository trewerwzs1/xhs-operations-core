# CLI 与环境诊断

## 当前真实能力

- 从当前目录向上发现项目根。
- 读取 `config/project.local.json`；不存在时读取示例配置。
- 拒绝绝对路径和逃出项目根的运行目录。
- 创建本地 runtime、logs 和 runtime reports 目录。
- 输出文本或 JSON 环境诊断结果。

## 安装后运行

```powershell
.\scripts\xhs-ops.ps1 doctor --project-root . --init-runtime
```

供 Codex 稳定读取的 JSON：

```powershell
.\scripts\xhs-ops.ps1 doctor --project-root . --init-runtime --format json
```

接收方不要设置 `PYTHONPATH`，也不要调用系统 Python 模块入口或未激活环境中的控制台入口。包装脚本固定使用项目 `.venv`。

## 配置优先级

1. CLI `--config` 显式路径。
2. `config/project.local.json`。
3. `config/project.example.json`。

本地配置不得提交版本库。

## 初始化

```powershell
.\scripts\install.ps1 -AccountId <安全账号ID> -ProfileName <独立profile名称>
```

安装器会调用固定初始化命令，生成本地配置、独立 profile marker 和 STOP。接收方不要再次手工运行 `setup init`。安装不会打开小红书，也不会允许平台访问。

浏览器登录、Campaign、Style Setup、DailyPlan、heartbeat、review、公开互动和 DM 均已有独立 CLI；以随包 Skill 的 `references/cli-contract.md` 为权威调用合同。
