' Start the local quote service without a console window.
' Double-click this file. The service only listens on 127.0.0.1.
' Keep it in the same folder as quote_server.ps1.

Option Explicit
Dim fso, shell, base, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & base & "\quote_server.ps1"""
shell.Run cmd, 0, False
