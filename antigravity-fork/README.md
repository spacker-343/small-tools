# Antigravity 原生 Fork 入口

跨版本自适应补丁工具，用于在 Antigravity 桌面端打开内置 Fork 与历史回复 Fork 开关。图标出现在 Antigravity 原生回复工具栏位置，菜单、历史切点、工作区判断、派生请求和跳转全部由原版实现处理。

| 操作 | 原生入口版 | 上一版独立目录补丁 |
| --- | --- | --- |
| 相同工作区 | 使用 Antigravity 原生 Fork，并继续用原目录 | 同样使用原生 Fork |
| 隔离工作区 | 根据 Antigravity 自己的开关和工作区类型显示。支持的 Git 项目可能提供新工作树 | 提供本地文件夹选择器，创建新项目并继承回复历史 |
| 历史回复 | 有有效原生切点的历史回复可以派生 | 对有有效切点的历史回复开放入口 |

普通 Windows 文件夹不会从原生菜单获得任意本地目录选择。此版本只恢复原有入口和行为。[Google 官方布局与工作区说明](https://antigravity.google/docs/settings#interface-layout)

`fork_workspace.js` 保留独立目录派生的参考代码，当前安装器不加载它。该片段依赖 Antigravity 前端 bundle 的内部符号，不能独立运行；接入其他版本前需要适配符号并验证目录隔离与派生行为。

## 升级后一键恢复 / 安装（推荐）

Antigravity 每次升级都会覆盖 `resources\bin\language_server.exe`。现在无需手动改版本号或手动备份/复制文件：

1. **完全退出 Antigravity**（确保没有残留的 `Antigravity.exe` 或 `language_server.exe` 进程）。
2. 在项目目录运行一键命令（或直接运行 `.\apply_fork.ps1`）：

```powershell
python .\fork_toolbar.py --apply
```

脚本会自动完成：
- 从 `resources\app.asar` 自动识别当前安装的 Antigravity 版本（不再写死版本号）；
- 自动将当前版本的原版 `language_server.exe` 备份到 `build\backups\language_server-<version>-official.exe`（若已是补丁版则不会覆盖原版备份）；
- 正则匹配并开启 `conversationForkingEnabled` 与 `conversationForkingHistoricalStep`，同时定位 Go 后端 `CascadeManager.allowForkWithSubagents`（`enable-fork-with-subagents`）放行携带 Swarm / 子 Agent 记录的会话 Fork，保持内嵌 ZIP 与二进制体积完全一致后原子替换生效；
- 如果未来某个新版本官方已经原生开启这两个开关，脚本会直接提示你无需再打补丁。

## 其他命令

仅构建候选文件（不修改安装目录）：

```powershell
python .\fork_toolbar.py
```

恢复当前版本的官方原版：

```powershell
python .\fork_toolbar.py --restore
```

运行单元测试：

```powershell
python -m unittest .\test_fork_toolbar.py -v
```
