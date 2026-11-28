# agy-update-guard（Update Guard）v1.2

零额度监控 Antigravity 官方更新、本机待安装安装包对 **`antigravity-fork` & `antigravity-chat-editor`** 的补丁兼容性、新版本**外审模型与安全护栏（`PolicyGuardian` / `PolicyProctor` / `LLMClassifier` / 本地 Skill 扫描过滤）**二进制差异及本机 `builtin` 快照变动；所有提醒 100% 在 Antigravity 应用内部呈现（无桌面系统弹窗、后台零账号交互），支持一键新建对话输出升级安全评级（`HOLD` / `CAUTION` / `SAFE`）。

## 核心能力与工作流

- **后台零账号接触与零额度守门**：
  - `sentinel.py --check` 与 `sidecar.json` 定时巡检仅通过标准库抓取公开网页（`changelog.md`、`models.md`）、检测本机 `Antigravity.exe` / `antigravity-updater` 缓存及对比 `builtin` 快照，**绝不在后台自动调用会话创建接口**。
  - 无变动时仅更新 `state.json` 的 `last_checked_at` 时间戳（0 Token 消耗）；检测到版本更新或内置配置差异时写入统一数据目录 `~/.gemini/antigravity/plugin_data/agy-update-guard/pending_notice.json`。
- **AGY 应用内静默消息横幅（`hooks.json` + `session_hook.py`）**：
  - 每次开启新对话时，在 `< 200ms` 内非阻塞读取 `pending_notice.json`：若存在未读提醒，通过 `SessionStart` 官方协议注入**仅用户可见、模型不可见（0 Token）**的应用内提示横幅，附带可点击插件入口链接 `customization://agy-update-guard` 与操作提示。
  - 若距离上次检查超过 4 小时且无活跃检查锁，自动以分离静默后台进程触发一次 `sentinel.py --check`。
- **原生 MCP App 交互卡片（一键新建对话审查）**：
  - 提供 `open_update_guard`（侧边栏 `+` 菜单 `Update Guard` 面板）与 `show_update_card`（对话流内联渲染交互卡片）。
  - 点击卡片主按钮 **「🚀 新建对话审查本次更新」**，通过官方 `app.sendMessage(..., { target: 'new' })` 将完整采集材料与预置评估 Prompt 一键送入新对话，并自动调用 `app_acknowledge_notice` 标记已读与推进基线快照。
  - 同步提供 **「💬 在当前对话审查」**、**「🔄 刷新检测（0 额度）」**、**「✓ 已知悉此版本」** 与一键复制兜底按钮。
- **生效范围区分与更新缓存预警**：
  - 自动扫描 `%LOCALAPPDATA%\antigravity-updater` 下的待安装 `.exe`；若存在待安装更新包且评估结论建议暂缓升级，提示用户切勿点击“重启更新”或直接重启客户端。
  - 客观区分【客户端本地版本变更（随客户端安装包生效）】与【云端模型及服务端配置变更（与本地客户端版本无关）】。

## 安装与启用

### 1. 运行安装脚本

```powershell
# 默认完整复制安装到 ~/.gemini/config/plugins/agy-update-guard（兼容桌面端目录扫描）
.\scripts\install_plugin.ps1

# 修改源码后就地同步更新安装
.\scripts\install_plugin.ps1 -Force
```

安装脚本会自动完成：
1. 调用 `agy plugin validate --strict` 严格校验插件清单与目录结构（0 errors / 0 warnings）；
2. 就地同步文件到 `$HOME\.gemini\config\plugins\agy-update-guard`（排除 `__pycache__` 等字节码缓存）；
3. 调用 `python sentinel.py --init-baseline` 在统一数据目录建立首次零额度基线快照；
4. 若当前会话环境变量存在 `ANTIGRAVITY_LS_ADDRESS` 与 `ANTIGRAVITY_CSRF_TOKEN`，自动刷新 `agy-update-guard_update-guard` MCP 服务。

> **安全说明**：脚本不会修改 `~/.gemini/config/config.json`，也不调用受限状态写入 RPC。

### 2. 在 Antigravity 客户端使用

1. 访问 `customization://agy-update-guard` 确认插件已启用，或直接点击插件详情页的预设提示词。
2. 点击对话右侧栏 `+` 菜单选择 **`Update Guard`** 打开交互状态卡片，或在对话中输入 `/update-audit` 触发即时评估。
3. 可选：打开 `sidecar://dashboard`（Automations）启用 **`Update Guard Sentinel`**，每 4 小时后台自动巡检一次。

## 常用命令行参数

巡检脚本位于 `sidecars/update-sentinel/sentinel.py`：

| 命令 | 说明 |
| --- | --- |
| `python sentinel.py --init-baseline` | 仅抓取并保存当前线上版本、模型段落与 `builtin` 快照（0 额度） |
| `python sentinel.py --check` | 后台零账号接触巡检；发现变动时写入 `pending_notice.json` 而不创建对话 |
| `python sentinel.py --acknowledge` | 将 `pending_notice.json` 标记为已读，并同步推进 `state.json` 与基线快照 |
| `python sentinel.py --dry-run --audit-latest` | 只读预检，输出完整采集信息与评估 Prompt，不写状态文件 |
| `python sentinel.py --dry-run --audit-latest --json` | 以 JSON 格式输出预检结果（`/update-audit` 技能默认使用） |
| `python sentinel.py --lock-updates` / `--unlock-updates` | 一键清理本地安装包并锁定/恢复自动更新与顶部更新按钮 |
| `python sentinel.py --save-snapshot [--export <path>]` | 打包当前客户端、Fork/Edit 补丁与免依赖一键部署脚本为跨机便携 `.zip` 快照 |
| `python sentinel.py --list-snapshots` | 列出已保存的客户端便携快照 |
| `python sentinel.py --rollback <zip_or_version>` | 退出客户端后一键从快照回滚恢复指定版本并自动锁死更新 |
| `python sentinel.py --audit-latest --manual-create-conversation` | 仅作命令行手动调试备用，显式通过原生对话通道发起评估 |

常用可选参数：`--data-dir <path>`、`--builtin-dir <path>`、`--model <model_id>`（或环境变量 `AGY_UPDATE_GUARD_MODEL`）、`--wait-seconds <sec>`。

## 独立校验本地 `builtin` 快照

```powershell
python .\scripts\builtin_snapshot.py diff --baseline "$HOME\.gemini\antigravity\plugin_data\agy-update-guard\snapshots\builtin_baseline.json"
```

## 运行单元测试

```powershell
python -B -m unittest discover -s ".\scripts" -p "test_*.py" -v
```
