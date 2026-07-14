param(
    [switch]$Once
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$localConfigPath = Join-Path $PSScriptRoot "local.json"
$projectConfigPath = Join-Path $PSScriptRoot "config.json"

if (-not (Test-Path $localConfigPath)) {
    throw ".autoloop\local.json was not found. Copy local.example.json and set autoloop_home."
}

if (-not (Test-Path $projectConfigPath)) {
    throw ".autoloop\config.json was not found."
}

$localConfig = Get-Content $localConfigPath -Raw | ConvertFrom-Json
$autoLoopHome = $localConfig.autoloop_home
$controller = Join-Path $autoLoopHome "controller.py"

if (-not $autoLoopHome) {
    throw "autoloop_home is not configured in .autoloop\local.json."
}

if (-not (Test-Path $controller)) {
    throw "AutoLoop controller was not found: $controller"
}

$arguments = @(
    $controller,
    "--project", $projectRoot,
    "--config", $projectConfigPath
)

if ($Once) {
    $arguments += "--once"
}

& py @arguments
exit $LASTEXITCODE
