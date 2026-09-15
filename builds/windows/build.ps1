$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $Root

$BuildVenv = Join-Path $Root ".venv-build-windows"
$Python = Join-Path $BuildVenv "Scripts/python.exe"
$Dist = Join-Path $Root "builds/windows/dist"
$Work = Join-Path $Root "builds/windows/work"

if (-not (Test-Path $Python)) {
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -eq $Launcher) {
        $Launcher = Get-Command python -ErrorAction SilentlyContinue
    }
    if ($null -eq $Launcher) {
        throw "Python 3.12 or newer was not found. Install Python from python.org and rerun."
    }
    & $Launcher.Source -m venv $BuildVenv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install . pyinstaller

New-Item -ItemType Directory -Force -Path $Dist, $Work | Out-Null
Remove-Item (Join-Path $Dist "JS8Mail.exe") -Force -ErrorAction SilentlyContinue

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name JS8Mail `
    --paths (Join-Path $Root "src") `
    --distpath $Dist `
    --workpath $Work `
    --specpath $Work `
    (Join-Path $Root "src/js8mail/tools/windows_launcher.py")

Write-Host "Built $Dist\JS8Mail.exe"
