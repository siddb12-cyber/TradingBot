' ============================================================
' stop_bot.vbs
' TradingBot Graceful Stop
'
' Double-click this to kill all running TradingBot Python processes.
' Safe to run even if the bot is not running.
' ============================================================

Option Explicit

Dim wsh, result

Set wsh = CreateObject("WScript.Shell")

' Kill pythonw.exe processes running main.py
' Uses taskkill — forceful but clean
result = wsh.Run("cmd /c taskkill /F /FI ""WINDOWTITLE eq pythonw*"" /IM pythonw.exe 2>nul", 0, True)

' Also kill any python.exe running main.py (if started via start.bat directly)
wsh.Run "cmd /c taskkill /F /FI ""COMMANDLINE eq *main.py*"" 2>nul", 0, True

WScript.Sleep 1000

MsgBox "TradingBot has been stopped." & vbCrLf & vbCrLf & _
       "If the bot was still running, check Telegram for the shutdown notification.", _
       64, "TradingBot Stopped"

Set wsh = Nothing
