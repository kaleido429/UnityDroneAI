param([ValidateSet('mock','ble')][string]$Mode = 'mock', [int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$bridgePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $bridgePython)) {
    throw 'Create the environment first: python -m venv .venv ; .venv\Scripts\python.exe -m pip install -r requirements.txt'
}
Push-Location $PSScriptRoot
try {
    $bridgeArgs = @('-m', 'blimp_bridge', '--port', $Port, '--config', (Join-Path $PSScriptRoot 'settings.example.json'))
    if ($Mode -eq 'mock') { $bridgeArgs += '--mock' }
    & $bridgePython @bridgeArgs
} finally { Pop-Location }
