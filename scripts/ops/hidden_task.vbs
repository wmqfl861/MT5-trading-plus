' Generic hidden launcher for scheduled tasks that run often (every minute).
'
' A task whose action is a console program -- python.exe, powershell.exe, even with
' -WindowStyle Hidden -- puts a console window on the interactive desktop every time it
' runs. wscript.exe has no console, and WScript.Shell.Run with window style 0 keeps the
' child completely hidden. Run also waits for the child, so the task's last result is the
' child's own exit code, and "do not start a new instance" sees the real run time.
'
' Action of the task:
'   wscript.exe //B //Nologo hidden_task.vbs <script.ps1> [arguments...]
' The script is run with:
'   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass
'               -WindowStyle Hidden -File <script.ps1> [arguments...]
'
' Nothing is ever shown: under wscript a message box would stop the task until somebody
' clicks it. Refusals are reported by exit code only, and nothing is started:
'   2  no arguments
'   3  the script does not exist
'   4  an argument contains a percent sign
'   5  an argument ends with a backslash
'   6  a statement of the launcher itself failed, or the script could not be started
' A percent sign: WScript.Shell.Run expands %NAME% in its command string, so a path with
' a percent pair in it would be changed on the way. A trailing backslash: powershell.exe
' takes the quote that closes such an argument for an escaped quote and merges it with the
' next one. Anything that cannot be passed as it is, is not passed at all.
' Anything else is the script's own exit code.

Option Explicit
Dim shell, files, command, i, argument, rc

If WScript.Arguments.Count < 1 Then WScript.Quit 2

For i = 0 To WScript.Arguments.Count - 1
    argument = WScript.Arguments(i)
    If InStr(argument, "%") > 0 Then WScript.Quit 4
    If Right(argument, 1) = "\" Then WScript.Quit 5
Next

' A run-time error leaves the host's exit code at 0, which under the task would read as a
' successful run. From here on every failing statement is noticed, and ends with 6.
On Error Resume Next
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
If Err.Number <> 0 Then WScript.Quit 6
If Not files.FileExists(WScript.Arguments(0)) Then WScript.Quit 3

' The system's own powershell.exe (GetSpecialFolder(1) is the System32 directory), not
' whatever PATH holds first.
command = files.BuildPath(files.GetSpecialFolder(1).Path, _
    "WindowsPowerShell\v1.0\powershell.exe") & _
    " -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File"
For i = 0 To WScript.Arguments.Count - 1
    command = command & " " & Chr(34) & WScript.Arguments(i) & Chr(34)
Next
If Err.Number <> 0 Then WScript.Quit 6

rc = shell.Run(command, 0, True)
If Err.Number <> 0 Then WScript.Quit 6
WScript.Quit rc
