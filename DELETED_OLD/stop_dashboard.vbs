' stop_dashboard.vbs
' =====================
' Stops the dashboard server (kills pythonw.exe running dashboard_server.py).
' The main bot (start_hidden.vbs) will NOT be killed by this script.

Dim WShell
Set WShell = CreateObject("WScript.Shell")

' Kill only the dashboard server process by matching script name
WShell.Run "taskkill /F /FI ""WINDOWTITLE eq dashboard_server*""", 0, True

' Fallback: use WMIC to find and kill by command line
WShell.Run "cmd /c wmic process where ""name='pythonw.exe' and commandline like '%dashboard_server%'"" delete", 0, True

Set WShell = Nothing
