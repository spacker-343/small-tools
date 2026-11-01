# Codex 跳转 Obsidian 笔记

在 Windows 版 Codex 中，将文件打开目标设置为此工具后，对聊天中的 Markdown 文件链接按 Ctrl 并点击，可将文件路径转换为 Obsidian 的链接，尝试打开对应笔记。普通点击可能仍在 Codex 内预览。

需要安装 Python 3、Obsidian，并在 Obsidian 中打开或登记包含笔记的仓库。其他类型的文件交给 Windows 默认应用处理。

## 配置

1. 确认 `pythonw.exe` 的绝对路径，并记下本目录中 `open_note.py` 的绝对路径。
2. 编辑用户目录下的 `.codex/config.toml`，在 `[desktop.open-in-target-preferences]` 中设置 `global = "custom:obsidian"`；若该表已存在，请修改其中的值，避免重复创建表。
3. 添加以下配置，替换其中两个路径。路径中的正斜杠可以在 Windows 使用：

   ```toml
   [desktop.custom_file_handlers.obsidian]
   label = "Obsidian"
   icon = "apps/vscode.png"
   command = "C:/你的Python目录/pythonw.exe"
   args = ["C:/你的工具目录/codex-obsidian-open/open_note.py"]
   input = "path"
   ```

4. 完全退出并重新打开 Codex，再对笔记文件链接按 Ctrl 点击。也可以从文件的“打开方式”菜单选择 Obsidian。

此配置会影响 Codex 的默认文件打开目标。这个脚本要求文件存在；Obsidian 根据文件绝对路径匹配已登记的仓库。不同 Codex 版本的链接处理行为可能不同。

参考：[Codex 自定义文件打开程序](https://learn.chatgpt.com/docs/config-file/config-advanced#add-custom-file-handlers)、[Obsidian URI](https://obsidian.md/help/Extending+Obsidian/Obsidian+URI)。
