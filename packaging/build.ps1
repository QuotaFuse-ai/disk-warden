# Build the unsigned Windows x64 development package (PyInstaller onedir).
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#   -Python <path to python.exe>   interpreter with packaging/requirements-build.txt
#   -Version <artifact version>    defaults to 0.9.0-beta.9

param(
    [string]$Python = "python",
    [string]$Version = "0.9.0-beta.9"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path $Python) -and -not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "Python interpreter not found: $Python"
}

$pyVersion = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($pyVersion -ne "3.12") {
    throw "Python 3.12 required for the pinned PyInstaller 6.x build (found $pyVersion)"
}
Write-Host "Using Python $pyVersion ($Python)"

& $Python -c "import PyInstaller, webview, psutil, pystray, PIL; print('deps ok')"
if ($LASTEXITCODE -ne 0) {
    throw "Build dependencies are missing. Run: $Python -m pip install -r packaging/requirements-build.txt"
}

$dist = Join-Path $root "dist"
$work = Join-Path $root "build"
$appDir = Join-Path $dist "DiskWarden"
$zipPath = Join-Path $dist "DiskWarden-$Version-win-x64.zip"

if (Test-Path $appDir) { Remove-Item -Recurse -Force $appDir }
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

& $Python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work packaging\DiskWarden.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$exe = Join-Path $appDir "DiskWarden.exe"
if (-not (Test-Path $exe)) { throw "Expected executable not found: $exe" }

# PyInstaller 6 collects datas into _internal/, but .NET resolves DiskWarden.exe.config
# next to the executable only.
Copy-Item (Join-Path $PSScriptRoot "DiskWarden.exe.config") (Join-Path $appDir "DiskWarden.exe.config") -Force

# The unsigned build triggers SmartScreen before any app UI can appear, so the
# first-run guidance ships inside the zip and is visible on extraction.
$firstRun = Join-Path $PSScriptRoot "START-HERE.txt"
Compress-Archive -Path $appDir, $firstRun -DestinationPath $zipPath -CompressionLevel Optimal
$hash = (Get-FileHash -Algorithm SHA256 $zipPath).Hash
$sizeMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)

Write-Host ""
Write-Host "Build complete"
Write-Host "  app dir : $appDir"
Write-Host "  zip     : $zipPath"
Write-Host "  size    : $sizeMb MB"
Write-Host "  sha256  : $hash"
