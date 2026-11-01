' Start-AutoMinimizeHidden.vbs
' Launch the watcher with a hidden window, so the guard itself never shows a console.
' Run THIS file from the scheduled task, not the .ps1 directly.
' Comments are ASCII on purpose: wscript reads .vbs as ANSI (system code page).
Option Explicit

Dim sh, fso, dir, cmd, ps
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

ps = "powershell.exe"
If Not fso.FileExists(sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe") Then
    ps = "pwsh.exe"
End If

cmd = """" & ps & """ -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & _
      dir & "\AutoMinimizeConsole.ps1"""

' Run argument 0 = hidden window, False = do not wait
sh.Run cmd, 0, False

Set sh = Nothing
Set fso = Nothing
