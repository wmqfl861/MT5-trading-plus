# recovery-20261119A 单参包装：任务只引本文件，参数固化于此
$ErrorActionPreference = 'Stop'
$m = '<workspace>'
& "$PSScriptRoot\start.ps1" -Mode Demo `
  -DeploymentSha cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5 `
  -ConfigDir "$m\data\demo-configs-consolidated" `
  -DataDir "$m\data" -LogDir "$m\data\demo" `
  -GatewayConfig "$m\gateway.json" `
  -SpreadModel "$m\data\costs\spread_model.json" `
  -ResearchCache "$m\data_research\costs\spread_model.json" `
  -PythonExe "$m\.venv\Scripts\python.exe"
exit $LASTEXITCODE
