' start_dashboard.vbs
' ====================
' Launches the TradingBot live dashboard server silently (no terminal window).
' Open http://localhost:8765 in your browser after running this.
'
' To stop: open Task Manager → find pythonw.exe running dashboard_server.py → End Task
' Or run:  stop_dashboard.vbs

Dim WShell
Set WShell = CreateObject("WScript.Shell")

' Resolve paths relative to this .vbs file location
Dim scriptDir
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)

Dim pythonExe
pythonExe = "pythonw.exe"

Dim serverScript
serverScript = """" & scriptDir & "\runtime\dashboard_server.py" & """"

Dim cmd
cmd = pythonExe & " " & serverScript

' Run hidden (0 = no window, False = don't wait)
WShell.Run cmd, 0, False

' Small delay then open browser
WScript.Sleep 1500

' Open browser to dashboard
WShell.Run "http://localhost:8765", 1, False

Set WShell = Nothing
WScript.Echo "Dashboard started! Open http://localhost:8765 in your browser."
