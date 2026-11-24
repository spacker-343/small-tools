# 小工具们

个人实用工具合集，每个文件夹都是一个独立工具。使用前请先阅读对应文件夹里的说明。

## 工具目录

| 工具 | 作用 |
| --- | --- |
| [AutoMinimizePopup](AutoMinimizePopup/README.md) | 自动收起符合规则的临时控制台窗口。 |
| [codex-vdesk-follow](codex-vdesk-follow/README.md) | 将 Codex、滴答清单、飞书或自定义应用固定到 Windows 的所有虚拟桌面。 |
| [codex-obsidian-open](codex-obsidian-open/README.md) | 在 Codex 中通过文件链接打开 Obsidian 笔记。 |
| [antigravity-chat-editor](antigravity-chat-editor/README.md) | 在 Antigravity 聊天气泡中编辑 AI 回复，渲染 Markdown，并将修改后的内容用于后续模型请求；支持压缩摘要后的续聊。 |
| [antigravity-fork](antigravity-fork/README.md) | 打开 Antigravity 内置的会话 Fork 与历史回复 Fork 入口，沿用原生菜单和工作区行为。 |

## Antigravity AI 回复编辑器

[antigravity-chat-editor](antigravity-chat-editor/README.md) 支持主聊天、全屏聊天，以及右侧子 Agent / Swarm Worker 的已完成 AI 文字回复。

- 在回复旁点击“编辑”，直接在气泡中修改并保存；保存立即生效，无需重启 Antigravity 或语言服务。
- 修改后的正文正常渲染 Markdown，再次编辑时保留原始 Markdown。保存成功保持静默，仅保存失败时在编辑框内提示。
- 后续模型请求使用修改后的正文。目标回复已经进入压缩摘要时，会按修改后的历史重建摘要，并保留编辑后回复的完整原文，供模型继续对话使用。
- 重建摘要会额外调用当前模型，长历史首次处理可能较慢；结果会缓存，支持后续压缩复用与分段中断恢复。

首次安装运行 [`antigravity-chat-editor/安装.cmd`](antigravity-chat-editor/安装.cmd)，普通安装需要先退出应用以接入加载补丁；安装后编辑和保存消息不需要重启。需要 Windows 和 Python 3.10+，日常使用不需要单独安装 Node 或 npm。

目前已在本机 Gemini 请求链路完成实际模型验证。插件不直接改写原生聊天数据库；原生导出和未经过插件的客户端可能仍显示原文。其他模型厂商或协议变化后的路径需要分别核验，详见[实际请求与兼容性说明](antigravity-chat-editor/README.md)。

## Antigravity 原生 Fork 入口

[antigravity-fork](antigravity-fork/README.md) 用于打开 Antigravity 内置的会话 Fork 与历史回复 Fork 开关。入口位于原生回复工具栏，历史切点、派生请求和跳转由 Antigravity 自己处理。

- 相同工作区沿用原目录；支持的 Git 项目可能提供新工作树，具体选项取决于 Antigravity 的开关和工作区类型。
- 普通 Windows 文件夹不会因此获得任意本地目录选择器。
- 自动识别当前安装版本并备份原版语言服务，不需要手动修改版本号；如果官方已经开启相关功能，会提示无需打补丁。

**安装、重新应用或恢复 Fork 补丁前，先完全退出 Antigravity 及其语言服务。** 在仓库根目录运行：

```powershell
python .\antigravity-fork\fork_toolbar.py --apply
```

安装完成后重新打开 Antigravity。官方更新会覆盖语言服务文件，更新后可按相同步骤重新应用补丁。恢复当前版本的官方原版：

```powershell
python .\antigravity-fork\fork_toolbar.py --restore
```

Fork 修改 `language_server.exe`，聊天编辑器的加载补丁修改 `app.asar`，两者可以配合使用。聊天编辑器在应用更新后可通过其目录中的 `启动Antigravity.cmd` 检查并恢复加载补丁。两款工具都依据当前安装结构检查兼容性；未来底层结构改变时仍可能需要适配。

## 使用与构建

本仓库不包含编译后的可执行文件。AutoMinimizePopup 可以通过安装脚本在本机编译，详见该工具的说明。
