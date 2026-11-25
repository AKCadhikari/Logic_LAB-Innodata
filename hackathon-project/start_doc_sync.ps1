[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Restart')]
    [string]$Action = 'Start',
    [int]$Port = 8765,
    [string]$Config,
    [string]$Storage,
    [string]$Logs,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Error 'The DocuSync Python environment is missing. Set it up using the project README, then run the launcher again.'
    exit 1
}

$launchArgs = @('-B', (Join-Path $PSScriptRoot 'project\startup.py'), $Action.ToLowerInvariant(), '--port', $Port)
if ($Config)  { $launchArgs += @('--config', $Config) }
if ($Storage) { $launchArgs += @('--storage', $Storage) }
if ($Logs)    { $launchArgs += @('--logs', $Logs) }
if ($NoBrowser) { $launchArgs += '--no-browser' }

& $python @launchArgs
exit $LASTEXITCODE
