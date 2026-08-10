param(
  [ValidateSet("zh", "en")][string]$Language = "zh",
  [string]$HostAddress = "127.0.0.1",
  [int]$Port = 8001,
  [ValidateSet("v1", "full")][string]$Edition = "v1",
  [switch]$NoInstall,
  [switch]$NoBrowserInstall,
  [switch]$Reload,
  [switch]$Check
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# Optional direct PowerShell configuration. Environment variables or .env
# are also supported. Never commit real keys.
# $env:SENSENOVA_IMAGE_BASE_URL = "https://example.com/v1"
# $env:SENSENOVA_IMAGE_API_KEY = "sk-..."
# $env:SENSENOVA_SEARCH_BASE_URL = "https://google.serper.dev"
# $env:SENSENOVA_SEARCH_API_KEY = "..."

$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) {
  $PythonArgs = @("-3")
} else {
  $Python = Get-Command python -ErrorAction SilentlyContinue
  $PythonArgs = @()
}
if (-not $Python) { throw "Python 3.12+ is required." }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "[SenseNova Present] Installing uv for the current user..."
  & $Python.Source @PythonArgs -m pip install --user uv
  $UserBase = (& $Python.Source @PythonArgs -m site --user-base).Trim()
  $env:Path = "$UserBase\Scripts;$env:Path"
}

$ArgsList = @(
  "$ProjectRoot\scripts\launch.py",
  "--language", $Language,
  "--host", $HostAddress,
  "--port", $Port,
  "--edition", $Edition
)
if ($NoInstall) { $ArgsList += "--no-install" }
if ($NoBrowserInstall) { $ArgsList += "--no-browser-install" }
if ($Reload) { $ArgsList += "--reload" }
if ($Check) { $ArgsList += "--check" }

& $Python.Source @PythonArgs @ArgsList
exit $LASTEXITCODE
