$ErrorActionPreference = 'Stop'
$m = '<workspace>'
$d = '<deploy>'
$py = "$m\.venv\Scripts\python.exe"
& $py -u "$PSScriptRoot\host.py" `
  --directory $d --sha cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5 `
  --data-dir "$m\data" --config-dir "$m\data\demo-configs-consolidated" `
  --python $py --gateway-config "$m\gateway.json" `
  --risk-config "$d\config\risk.toml" --sessions-config "$d\config\sessions.toml" `
  --spread-model "$m\data\costs\spread_model.json" --log-dir "$m\data\demo"
exit $LASTEXITCODE
