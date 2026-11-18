param(
    [string]$Python = 'python',
    [string]$VenvPath = '.venv',
    [string]$DependencyDirectory = ''
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $DependencyDirectory) {
    $DependencyDirectory = Join-Path $PSScriptRoot 'dependencies'
    if (-not (Test-Path -LiteralPath $DependencyDirectory)) {
        $DependencyDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) 'build-dependencies'
    }
}
$DependencyDirectory = (Resolve-Path -LiteralPath $DependencyDirectory).Path
$manifest = Get-Content -LiteralPath (Join-Path $DependencyDirectory 'manifest.json') -Raw | ConvertFrom-Json
foreach ($entry in $manifest.files.PSObject.Properties) {
    $file = Join-Path $DependencyDirectory $entry.Name
    if (-not (Test-Path -LiteralPath $file) -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower() -ne $entry.Value) {
        throw "Dependency is missing or damaged: $($entry.Name). Re-extract the complete release ZIP."
    }
}
if ((Get-FileHash -LiteralPath requirements-lock.txt -Algorithm SHA256).Hash.ToLower() -ne $manifest.requirements_sha256) {
    throw 'Requirements and dependency bundle do not match.'
}
$venvPython = Join-Path $VenvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
        throw 'Install the bundled build-dependencies\installers\python-3.14.2-amd64.exe, then rerun with -Python <python.exe path>.'
    }
    & $Python -c "import sys,struct; assert sys.version_info[:2] == (3,14) and struct.calcsize('P') == 8, 'Python 3.14 x64 required'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.14 x64 required.' }
    & $Python -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create build environment.' }
}
$venvPython = (Resolve-Path -LiteralPath $venvPython).Path
& $venvPython -c "import sys,struct; assert sys.version_info[:2] == (3,14) and struct.calcsize('P') == 8, 'Python 3.14 x64 required'"
if ($LASTEXITCODE -ne 0) { throw 'Existing build environment must use Python 3.14 x64.' }
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
& $venvPython -m pip install --no-index --find-links (Join-Path $DependencyDirectory 'wheelhouse') -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Offline dependency installation failed.' }
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency compatibility check failed.' }
$ffmpeg = Join-Path $PSScriptRoot 'third_party\ffmpeg\ffmpeg.exe'
if (-not (Test-Path -LiteralPath $ffmpeg)) {
    Copy-Item -LiteralPath (Join-Path $DependencyDirectory 'ffmpeg\ffmpeg.exe') -Destination $ffmpeg
}
New-Item -ItemType Directory -Force -Path work\tmp,work\pyinstaller-cache | Out-Null
$env:TEMP = Join-Path $PSScriptRoot 'work\tmp'
$env:TMP = $env:TEMP
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot 'work\pyinstaller-cache'
$env:PATH = (Join-Path $DependencyDirectory 'node') + [IO.Path]::PathSeparator + $env:PATH
& (Join-Path $PSScriptRoot 'webview_host\build.ps1') -DependencyDirectory $DependencyDirectory
& $venvPython -m unittest discover -s tests -t . -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& $venvPython -m PyInstaller --noconfirm XDownloader.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Copy-Item -LiteralPath dist\XDownloader.exe -Destination XDownloader.exe
Write-Output 'Built XDownloader.exe with offline dependencies.'
