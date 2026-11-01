# codex-vdesk-follow

将应用固定到 Windows 的所有虚拟桌面。主入口是 `Pin-AllDesktops.ps1`，默认目标为 Codex，也提供滴答清单和自定义进程入口。

## 使用

在本目录打开 **Windows PowerShell 5.1**，确保目标应用已打开可见窗口，然后运行：

```powershell
# 固定 Codex 到所有虚拟桌面
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Pin-AllDesktops.ps1

# 只查询当前状态
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Pin-AllDesktops.ps1 -Status -Json

# 取消固定 Codex
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Pin-AllDesktops.ps1 -Unpin

# 固定滴答清单，或同时固定两个应用
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Pin-AllDesktops.ps1 -Target ticktick
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Pin-AllDesktops.ps1 -Target both

# 自定义目标进程，使用不带 .exe 的进程名正则
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Pin-AllDesktops.ps1 -Target custom -ProcessMatch '^MyApp$'
```

请使用 `powershell.exe`，脚本不支持 PowerShell Core（`pwsh`）。预设目标包含安装路径和 Shell AppID 校验，其他安装方式可以使用自定义进程匹配，或通过 `-AppId` 指定应用标识。

脚本通过固定应用实现跨桌面显示，执行后退出，无需常驻轮询。主脚本需与 `VDeskInterop.cs` 放在同一目录。

## 文件

| 文件 | 用途 |
| --- | --- |
| `Pin-AllDesktops.ps1` | 固定、取消固定和查询状态的主入口。 |
| `VDeskInterop.cs` | Windows 虚拟桌面及 Shell COM 接口封装，运行时由脚本加载。 |
| `Pin-TickTickAllDesktops.ps1` | 滴答清单快捷入口。 |
| `Follow-CurrentDesktop.ps1` | 兼容入口，默认同时处理 Codex 和滴答清单；`IntervalMs` 和 `Restore` 参数不再生效。 |
| `vd-selfcheck.ps1` | 查询系统版本和虚拟桌面接口状态。 |

诊断命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vd-selfcheck.ps1
```
