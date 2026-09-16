' Windowless launcher for the MT5 monitor autostart task.
'
' Registered by install_mt5bot_autostart.bat as the action of the "MT5TelegramBot"
' task. Unlike the Claude bot, the monitor starts ACTIVE (no pause concept:
' its whole purpose is notifications). Resolves repo root as this script's
' parent folder, launches pythonw.exe (no console) fire-and-forget.
Option Explicit

Dim sh, fso, dir, root, py
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

dir = fso.GetParentFolderName(WScript.ScriptFullName)   ' ...\mt5bot
root = fso.GetParentFolderName(dir)                     ' repo root (venv there)
sh.CurrentDirectory = root

py = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
    py = root & "\.venv\Scripts\python.exe"
End If

' Window style 0 = hidden, bWaitOnReturn = False -> don't block, just launch.
sh.Run """" & py & """ -u mt5bot\mt5_bot.py", 0, False
