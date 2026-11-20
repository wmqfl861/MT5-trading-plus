' silent launcher for live_supervisor2 scheduled task
' replaces direct powershell.exe launch: powershell -WindowStyle Hidden still
' flashes its console window for a moment; wscript has no console at all and
' Shell.Run style 0 keeps the child fully hidden. Exit code is passed through.
Dim sh, rc
Set sh = CreateObject("WScript.Shell")
rc = sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""<workspace>\data\ops-evidence\recovery-20261119A\sup-wrapper.ps1""", 0, True)
WScript.Quit rc
