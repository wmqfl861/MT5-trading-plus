$a = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/supervisor-wrapper.cmd" -WorkingDirectory "C:/Users/Administrator/.zcode/workspace/demo-deploy-cd6fc4a"
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30) -RepetitionInterval (New-TimeSpan -Minutes 1)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 55) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "MT5Quant-supervisor" -Action $a -Trigger $t -Settings $s -Force
Write-Output SUPERVISOR-OK
