# AutoMinimizePopup · 黑框自动收起

## 后台隐藏窗口被意外显示的修复（2026-11-01）

隔离测试确认旧版存在反向显示问题：一个从未请求显示、保持隐藏的测试窗口，被创建事件里的“预摁”改成最小化；随后的撤销路径又主动还原显示，并且没有取消原来的收起队列。后台程序即便已请求隐藏，也可能被这个工具干扰。

现在创建和显示事件都只处理已经可见且完整匹配规则的窗口，不再提前最小化隐藏窗口，也移除了主动还原的撤销路径。排队期间若窗口被所属程序隐藏，立即放弃处理。此修复不承诺消除程序自己创建可见窗口时的首次闪屏。

## 使用边界与性能修正（2026-11-01）

本工具自动处理新出现的控制台窗口，适合不想逐个修改开发脚本启动方式的场景。但它在窗口创建后才介入，**不能保证完全不闪**；下文旧版的耗时记录也不代表零延迟。`run-hidden` 等启动器能让指定进程从启动时不创建控制台，但需要修改启动入口，不能自动接管所有开发工具。

本次修复了常驻程序的重复扫描：窗口消息会提前唤醒等待，旧版却在每次唤醒后立即扫描桌面；现在只有实际经过 `IntervalMs` 才扫描。创建、显示事件和扫描也会先筛选窗口类，再读取匹配窗口的标题和进程信息。待收起窗口与短时焦点观察按同一计时推进，保留原来的自动处理方式，无须配置每个脚本。

本机对比使用相同循环方式创建、销毁不可见的非控制台窗口，worker 以只记录模式运行约 6 秒：

| 指标 | 修改前 | 修改后 |
| --- | --- | --- |
| 测试期间生成的窗口数 | 215 | 315 |
| 桌面扫描次数 | 184 | 4 |
| 进程累计 CPU 耗时 | 1.5781 秒 | 0.2344 秒 |

这些数据用于验证事件密集时的重复扫描已减少，不代表日常固定 CPU 占用，也不代表闪屏已消除。独立的窗口回归测试确认：新窗口仍自动收起，启动前已有窗口和其他窗口类不被处理。

## 为什么做这个

写程序的时候，最烦的不是报错，是**黑框**。

调一个脚本、跑一个打包、点一下某个 `.bat`、让程序 `system()` / `Start-Process` 起个 `cmd`——屏幕上"啪"地弹出一个 `C:\WINDOWS\system32\cmd.exe` 的小黑框，顺手把焦点从你正在打的字里抢走，还在任务栏上给你留一个按钮。它自己跑完还会关，但那 0.3 秒足够打断一次思路，一天几百次。

我不想杀掉它（很多黑框后面的进程是真在干活，杀了活就废了），也不想让它出现在任务栏碍眼。**我只要它"别露脸"**。

于是有了这个常驻小工具：黑框一出现就把它**最小化**（不是关闭），并且把窗口从**任务栏和 Alt+Tab 里摘掉**，同时把**焦点还给你刚才那个窗口**。进程照常跑、输出照常写、管道照常读。

## 它做了什么

| 动作 | 做法 | 影响 |
| --- | --- | --- |
| 收起 | `ShowWindow(SW_SHOWMINNOACTIVE)` | 只最小化，**不关闭、不结束进程** |
| 创建事件检查 | 仅处理创建时已经可见且匹配规则的窗口，隐藏窗口留给后续 SHOW 事件 | 避免主动显示后台隐藏窗口，不能保证零闪 |
| 藏任务栏 | 加 `WS_EX_TOOLWINDOW`、清掉互斥的 `WS_EX_APPWINDOW`，再 `SetWindowPos(SWP_FRAMECHANGED)` 让 shell 刷新 | 只改"露脸方式"，任务栏无按钮、Alt+Tab 无此项 |
| 还焦点 | `SetForegroundWindow` → `AttachThreadInput` + `BringWindowToTop` + `SetFocus` → `keybd_event(VK_MENU)` 解锁 → `SwitchToThisWindow`，逐级兜底 | 焦点回到黑框弹出来之前那个窗口 |
| 不跟你抢 | 你手动把某个黑框点回来之后，它被记成"已确认"，之后**不再**自动最小化它 | 你能正常用那个黑框 |

只认 `ConsoleWindowClass`（经典 conhost 黑框）+ 标题正则，**Windows Terminal、VS Code 内嵌终端不会被动到**；脚本启动时就已经开着的窗口一律不碰。

## 安装

```powershell
cd AutoMinimizePopup
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-AutoMinimize.ps1
```

它会：

1. 把文件复制到 `%LOCALAPPDATA%\AutoMinimizePopup`（已有的 `autominimize.json` 不覆盖）；
2. 用 `.NET Framework` 自带的 `csc.exe` 现场编译 `AutoMinimizeWorker.exe`（没有 csc 就用随包附带的预编译 exe；再不行退回 PowerShell 版）；
3. 注册计划任务 `AutoMinimizePopupConsoles`（登录时启动、无窗口、`IgnoreNew` 防多开）；
4. 立刻拉起后台实例。

管理员账户下计划任务默认 `RunLevel=Highest`（登录时**不会再弹 UAC**）；想退回受限权限加 `-LimitedWorker`。

## 卸载

```powershell
# 只停服务和计划任务，保留文件
powershell -NoProfile -File .\Install-AutoMinimize.ps1 -Uninstall

# 连安装目录一起清掉
powershell -NoProfile -File .\Install-AutoMinimize.ps1 -Uninstall -RemoveFiles
```

卸载前会自动执行一次 `/reveal`，把之前藏起来的黑框放回任务栏——否则 exe 删掉之后就再也没法反悔了。

## 验证

```powershell
# 造几个真弹框，看它们有没有被自动最小化
powershell -NoProfile -ExecutionPolicy Bypass -File .\Test-Popup.ps1

# 只列出当前所有控制台窗口（类名 / 标题 / pid / 是否已最小化），用来确认某个黑框的类名
powershell -NoProfile -File .\Test-Popup.ps1 -List
```

隐藏窗口保护的回归检查：

```powershell
powershell -NoProfile -File .\Test-HiddenWindowRegression.ps1
```

测试使用独立窗口类、独立常驻实例和离屏窗口，检查隐藏窗口、自动收起、已有窗口、白名单、不匹配窗口和手动还原六项行为。

## 黑窗来源监控

`PopupSourceMonitor.cs` 是独立的只读监控器，不显示、隐藏或结束其他程序。它记录窗口事件、标题、可见状态、进程命令行和父进程链，也记录新出现的其他顶层窗口，便于排查不属于经典控制台的临时窗口。

在项目目录中编译并启动：

```powershell
& "$env:SystemRoot\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /optimize+ /codepage:65001 /r:System.Management.dll /r:System.Web.Extensions.dll /out:PopupSourceMonitor.exe .\PopupSourceMonitor.cs
Start-Process -FilePath "$PWD\PopupSourceMonitor.exe" -Verb RunAs -WindowStyle Hidden
```

管理员权限用于订阅系统进程启动事件；普通权限下无法订阅时，状态中会明确标记不可用。输出位于 `%LOCALAPPDATA%\AutoMinimizePopup\monitor\时间戳目录`：

| 文件 | 内容 |
| --- | --- |
| `events.jsonl` | 原始进程和窗口事件；按约 8 MiB 轮转，保留当前文件和四份历史文件 |
| `summary.txt` | 定期更新的来源汇总，窗口类与来源链分别记录 |
| `status.json` | 运行状态、计划结束时间及事件丢弃计数 |
| `stopped.txt` | 实际停止时间 |

默认运行 12 小时后自动退出。可用 `--hours 1` 缩短时间、用 `--output "C:\路径"` 指定目录，或在本次输出目录创建 `monitor.stop` 文件提前停止。

后台控制台进程启动不等于实际闪屏，普通应用浮窗也会被记录。异步事件回调中的可见状态不能精确还原屏幕显示时长；进程实例身份无法确认时保留未知，不把复用的 PID 当成已确认来源。日志包含本机命令行和路径，应保留在本机。

## 一次性命令（`AutoMinimizeWorker.exe`）

| 命令 | 作用 |
| --- | --- |
| `/list` | 列出当前所有控制台窗口及其状态 |
| `/reveal` | **反悔用**：把被藏起来的黑框放回任务栏并还原显示 |
| `/probe:<hwnd>` | 诊断：对单个窗口试一次"收"，打印 `ShowWindow` 返回值、错误码、前后 `iconic` / `toolwindow` 状态 |
| `/dryrun` `/once` | 只记录不操作 / 扫一轮就退出 |

`/list`、`/reveal`、`/probe` 的输出走 stderr（常驻件是 winexe，没有控制台），在 PowerShell 里要这样收：

```powershell
$exe = "$env:LOCALAPPDATA\AutoMinimizePopup\AutoMinimizeWorker.exe"
Start-Process -FilePath $exe -ArgumentList '/list' -Wait -NoNewWindow `
    -RedirectStandardError "$env:TEMP\list.txt"
Get-Content "$env:TEMP\list.txt"
```

## 配置

编辑 `%LOCALAPPDATA%\AutoMinimizePopup\autominimize.json`（支持 `//` 整行注释），改完重跑一次 `Install-AutoMinimize.ps1` 生效。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `IntervalMs` | `1500` | 兜底扫描间隔。平时靠事件驱动，这个只是"万一事件没收到"的保险 |
| `IncludeClasses` | `["ConsoleWindowClass"]` | 只处理这些窗口类 |
| `TitlePatterns` | cmd/bat/conhost 等 5 条正则 | 命中任意一条才认定是"临时弹框" |
| `WhitelistTitlePattern` | `""` | 命中规则但命中这条正则的，放过 |
| `ExcludeProcessNames` | `WindowsTerminal,Code,Cursor,wt` | 这些进程拥有的窗口一律不动 |
| `IgnoreExistingWindows` | `true` | 启动时已开着的窗口永远不碰 |
| `MinimizeOnceOnly` | `true` | 每个窗口只自动收一次，你点回来后不再抢 |
| `RestoreFocus` | `true` | 收起后把焦点还给原来的窗口 |
| `MinimizeOnCreate` | `true` | 创建时也检查已可见且完整匹配的窗口，不操作隐藏窗口 |
| `HideFromTaskbar` | `true` | 同时从任务栏和 Alt+Tab 摘掉 |
| `LogPopupSource` | `true` | 日志里记录弹框进程的命令行和父进程，方便查"是谁在弹黑框" |
| `LogFile` | `""` | `''`=同目录 `autominimize.log`；`'none'`=不写；也可写绝对路径 |

布尔值写错（比如 `ture`）时会**退回默认值**，不会静默当成 `false`。

## 实测结果

测试机器：Windows 11 zh-CN，PowerShell 5.1。测量方式：`Start-Process cmd.exe` 造真弹框，用 Win32 API 逐毫秒采样 `IsIconic` / `IsWindowVisible` / `GetForegroundWindow` / 任务栏可见顶层窗口枚举。

 | **本工具（C# exe）** | **0.63–1.25% 单核** | **22.5–23.3 MB** | **32–157 ms** |

| 方案 | 常驻 CPU | 常驻内存 | 收起延迟 |
| --- | --- | --- | --- |
| 60ms 定时轮询 | ≈18% 单核 | ≈85 MB | 快，但白烧 CPU |
| PowerShell 事件驱动 | 5.9% | 95 MB | 3–20 s（PS 宿主拿不到 `SetWinEventHook` 回调，只能靠轮询兜底） |
| **本工具（C# exe）** | **0.63–1.25% 单核** | **22.5–23.3 MB** | **55–157 ms** |

### 弹框逐项实测

单轮 3 次弹框，共 3 轮：

| 轮次 | 收起耗时 | 黑框占用前台 | 焦点回到原窗口 | 任务栏黑框按钮 |
| --- | --- | --- | --- | --- |
| 第 1 轮 | 32 / 79 / 99 ms | 143 / 79 / 119 ms | 3/3 | 0 → 0 |
| 第 2 轮 | 105 / 157 / 87 ms | 214 / 97 / 140 ms | 3/3 | 0 → 0 |
| 第 3 轮 | 60 / 67 / 75 ms | 121 / 147 / 119 ms | 3/3 | 0 → 0 |

同一时刻 `GetForegroundWindow()` 的实测基线：黑框出现后焦点占用 79–214 ms 内交还原窗口，**9/9 次都回到了黑框弹出来之前那个窗口**（`toolwindow=True` 全程成立，任务栏黑框按钮数全程为 0）。

### 隐藏不影响干活

让黑框在**被藏起来的状态下**跑一个批处理（`ping` 计时后 `echo done > marker.txt`）：

```
隐藏中: iconic=True toolwindow=True
隐藏状态下写出 marker = True ; 此刻进程存活 = True
```

即：窗口看不见，但脚本正常执行、正常输出文件。

### 反悔手段有效

`/reveal` 之后按 200 ms / 1 s / 2 s / 4 s 采样，窗口一直保持可见、不被重新藏起来：

```
/reveal: 已放回 hwnd=0x71C0AA0 pid=12616 进程=cmd 标题='C:\WINDOWS\system32\cmd.exe'
reveal 后累计 ~200ms : IsWindow=True iconic=False toolwindow=False
reveal 后累计 ~1000ms: IsWindow=True iconic=False toolwindow=False
reveal 后累计 ~2000ms: IsWindow=True iconic=False toolwindow=False
reveal 后累计 ~4000ms: IsWindow=True iconic=False toolwindow=False
```

### 不跟你抢窗口

手动把一个已收起的黑框点回来，等 3 秒：`iconic=False` —— 不会被再次自动最小化。

## 已知限制

**Windows 的 UIPI 规则：低完整性进程不能操作高完整性窗口，而且是静默失效**（标题读得到，`ShowWindow` 也返回 `true`，就是没作用）。

- 管理员账户下计划任务默认 `RunLevel=Highest`，普通脚本弹的（中等完整性）和管理员脚本弹的（高完整性）黑框都能收。
- 非管理员账户下，管理员进程弹的黑框收不动。
- 系统级（System / TrustedInstaller 完整性）进程弹的控制台窗口收不动。

排查方法（不要猜，直接量）：

```powershell
# 1. 看这个黑框的 hwnd
AutoMinimizeWorker.exe /list
# 2. 对同一个 hwnd 做一次 /probe：iconic 从 False 变 True 就是收到了；
#    一直是 False 且 err=0，基本就是完整性不够
AutoMinimizeWorker.exe /probe:<hwnd>
```

**根治办法是别让它弹**（推荐同时做）：自己代码里起子进程时加上不建窗口的标志——

- .NET：`ProcessStartInfo { CreateNoWindow = true, UseShellExecute = false }`
- PowerShell：`Start-Process -WindowStyle Hidden`
- C/C++：`CreateProcess(..., CREATE_NO_WINDOW, ...)`
- Python：`subprocess.Popen(..., creationflags=subprocess.CREATE_NO_WINDOW)`

## 实现要点（踩过的坑）

- **必须编译成 exe**：PowerShell 宿主拿不到 `SetWinEventHook` 回调，只能靠轮询，开销和延迟都差一个数量级。
- **回调里绝不 `Sleep`、不连环重试**：跨进程 `SetWindowPos` 要等消息泵，回调里睡眠会把泵卡死，越重试越乱。改成"回调只登记，真正的按压在 `PressTick` 里每 10ms 推进一步"。
- **样式和最小化不能同一轮做**：实测同一轮里 `ShowWindow` + `SetWindowPos(SWP_FRAMECHANGED)` 会把刚设好的最小化顶回可见状态，连摁 30 次都摁不住。改成两段式：先确认 `IsIconic` 了，下一轮才补任务栏样式。
- **焦点归还目标要提前记账**：按压那一刻黑框可能还没拿到前台（窗口管理器还在切换），只看"当前前台 == 黑框"会漏掉归还，用户看到的就是"焦点跑到别处了"。所以每个黑框在创建时就记下"弹出来之前谁在前台"。
- **`WS_EX_APPWINDOW` 与 `WS_EX_TOOLWINDOW` 互斥**，加后者之前要清掉前者，否则 shell 不认。

## 文件

| 文件 | 说明 |
| --- | --- |
| `AutoMinimizeWorker.cs` | 常驻件源码（C#，.NET Framework 4.0，无第三方依赖） |
| `AutoMinimizeWorker.exe` | 预编译产物，供没有 `csc.exe` 的机器直接用 |
| `Install-AutoMinimize.ps1` | 安装 / 重装 / 卸载 |
| `autominimize.json` | 配置 |
| `AutoMinimizeConsole.ps1` | PowerShell 兜底版（csc 不可用时仍能跑） |
| `Start-AutoMinimizeHidden.vbs` | 兜底版的静默启动器 |
| `Test-Popup.ps1` | 自测脚本 |
| `Test-HiddenWindowRegression.ps1` | 隔离的隐藏窗口保护回归测试 |
| `PopupSourceMonitor.cs` | 独立只读的窗口与进程来源监控器 |
