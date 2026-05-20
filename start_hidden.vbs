' ============================================================
' start_hidden.vbs
' TradingBot Silent Launcher
'
' Double-click this file to start TradingBot with NO terminal window.
' The bot runs completely in the background using pythonw.exe.
'
' Logs are written to: TradingBot\logs\trading.log
'
' To stop the bot: double-click stop_bot.vbs
' ============================================================

Option Explicit

Dim wsh, botDir, batFile

Set wsh    = CreateObject("WScript.Shell")
botDir     = "C:\Users\siddh\Downloads\HK\TradingBot"
batFile    = botDir & "\start_background.bat"

' Run the batch file silently (window style 0 = hidden)
' False = don't wait for completion (fire and forget)
wsh.Run "cmd /c """ & batFile & """", 0, False

' Brief pause then notify
WScript.Sleep 2000
MsgBox "TradingBot is running in the background." & vbCrLf & _
       "Logs: " & botDir & "\logs\trading.log" & vbCrLf & vbCrLf & _
       "To stop: run stop_bot.vbs", _
       64, "TradingBot Started"

Set wsh = Nothing
