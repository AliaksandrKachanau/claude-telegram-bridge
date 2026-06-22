' Windowless launcher for the Task Scheduler autostart task.
'
' Sets BOT_START_PAUSED=1 so the bot comes up PAUSED (safe default): the owner must
' send /resume from Telegram before any Claude work runs. Launches pythonw.exe (no
' console window) and exits immediately (fire-and-forget). No restart loop — the bot
' already retries transient network errors internally, and hard crashes are rare.
'
' Registered by install_autostart.bat as the action of the "ClaudeTelegramBot" task.
Option Explicit

Dim sh, fso, dir, py
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve this script's own folder so it works regardless of the task's working dir.
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

py = dir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
    ' Fallback to console python if pythonw is missing (shouldn't happen in a venv).
    py = dir & "\.venv\Scripts\python.exe"
End If

' Mark this launch as auto-started -> bot starts paused until /resume.
sh.Environment("Process").Item("BOT_START_PAUSED") = "1"

' Window style 0 = hidden, bWaitOnReturn = False -> don't block, just launch.
sh.Run """" & py & """ -u bot.py", 0, False
