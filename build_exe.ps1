$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot
$releaseRoot = Join-Path $PSScriptRoot "release"
$buildRoot = Join-Path $PSScriptRoot "build"
$webRoot = (Resolve-Path (Join-Path $PSScriptRoot "web")).Path
New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null

py -m pip install --disable-pip-version-check -r .\build-requirements.txt
$pyinstallerArgs = @(
  "--noconfirm"
  "--clean"
  "--onefile"
  "--windowed"
  "--name"
  "ClashNodeMonitor"
  "--distpath"
  $releaseRoot
  "--workpath"
  (Join-Path $buildRoot "pyinstaller")
  "--specpath"
  $buildRoot
  "--add-data"
  "$webRoot;web"
  (Join-Path $PSScriptRoot "clash_node_monitor.py")
)
& py -m PyInstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host ("Built " + (Join-Path $releaseRoot "ClashNodeMonitor.exe"))
