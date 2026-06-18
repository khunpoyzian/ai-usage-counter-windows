' One-shot: creates Desktop + Startup shortcuts for the dashboard.
Set sh = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

Sub MakeLink(folder)
    Set lnk = sh.CreateShortcut(folder & "\Claude Usage.lnk")
    lnk.TargetPath = "wscript.exe"
    lnk.Arguments = """" & scriptDir & "run_hidden.vbs"""
    lnk.WorkingDirectory = scriptDir
    lnk.WindowStyle = 1
    lnk.Description = "Claude Usage Dashboard"
    lnk.IconLocation = "C:\Users\khunpoyzian\AppData\Local\Programs\Python\Python311\pythonw.exe,0"
    lnk.Save
    WScript.Echo "created: " & folder & "\Claude Usage.lnk"
End Sub

MakeLink sh.SpecialFolders("Desktop")
MakeLink sh.SpecialFolders("Startup")
