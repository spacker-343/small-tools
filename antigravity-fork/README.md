# Antigravity 原生 Fork 入口

适配 Antigravity 桌面版 **2.33.0**。候选补丁只在本地桌面端打开内置 Fork 和历史回复 Fork 开关。图标出现在 Antigravity 原本的回复工具栏位置。菜单、历史切点、工作区判断、派生请求和跳转全部由原版实现处理。

| 操作 | 原生入口版 | 上一版独立目录补丁 |
| --- | --- | --- |
| 相同工作区 | 使用 Antigravity 原生 Fork，并继续用原目录 | 同样使用原生 Fork |
| 隔离工作区 | 根据 Antigravity 自己的开关和工作区类型显示。支持的 Git 项目可能提供新工作树 | 提供本地文件夹选择器，创建新项目并继承回复历史 |
| 历史回复 | 有有效原生切点的历史回复可以派生 | 对有有效切点的历史回复开放入口 |

普通 Windows 文件夹不会从原生菜单获得任意本地目录选择。此版本只恢复原有入口和行为。[Google 官方布局与工作区说明](https://antigravity.google/docs/settings#interface-layout)

## 构建与安装

在项目目录使用 Python 3.10 或更新版运行以下命令。

```powershell
python .\fork_toolbar.py
```

输出为 `build\language_server-native-fork-history.exe`。可以用 `--install` 指定安装根目录，用 `--output` 指定新的输出路径。构建脚本检查版本和替换位置。候选文件生成在安装目录以外。

候选文件是本地构建的非官方改版。安装需要先退出 Antigravity，把原版 `resources\bin\language_server.exe` 备份到安装目录外，再替换该文件。已安装第一版入口补丁时，保留原版备份并更换候选文件即可。回滚时退出应用并换回原版。更新 Antigravity 后须按实际版本重新适配。
