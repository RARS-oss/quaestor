' Launches watchdog.ps1 with no console window at all.
'
' Task Scheduler's -WindowStyle Hidden is not enough on its own: it creates the
' console host for powershell.exe first and only then lets PowerShell hide it,
' so a window flashes for a fraction of a second on every run. At a check every
' five minutes for the length of the contest that is roughly a thousand flashes.
'
' wscript.exe creates no console, so launching PowerShell from here with window
' style 0 means there is never a window to flash. The watchdog's own logic is
' untouched; only the way it is started changes.

Option Explicit

Dim fso, shell, scriptDir, psScript, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Resolve watchdog.ps1 next to this file, so the pair can be moved together.
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = fso.BuildPath(scriptDir, "watchdog.ps1")

If Not fso.FileExists(psScript) Then
    WScript.Quit 2
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & psScript & """"

' 0 = hidden window, False = do not wait for it to finish.
shell.Run cmd, 0, False
