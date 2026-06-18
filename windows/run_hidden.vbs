' Launches the Claude Usage Dashboard with no console window.
Set sh = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.CurrentDirectory = scriptDir
sh.Run "pythonw.exe """ & scriptDir & "claude_usage_dashboard.py""", 0, False
