$deploy = 'C:/Users/Administrator/.zcode/workspace/demo-deploy-cd6fc4a'
$main = 'C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13'
$venv = "$main/.venv/Scripts/python.exe"
$gw = "$main/gateway.json"
$data = "$main/data"
$configs = "$main/data/demo-configs-consolidated"
$risk = "$deploy/config/risk.toml"
$sessions = "$deploy/config/sessions.toml"
$spread = "$main/data/costs/spread_model.json"
$research = "$main/data_research/costs/spread_model.json"
$logs = "$main/data/demo"
$sha = 'cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5'

& "$deploy/scripts/demo_run.ps1" -Mode Demo -DeploymentSha $sha -DataDir $data -ConfigDir $configs -PythonExe $venv -GatewayConfig $gw -RiskConfig $risk -SessionsConfig $sessions -SpreadModel $spread -ResearchCache $research -LogDir $logs
