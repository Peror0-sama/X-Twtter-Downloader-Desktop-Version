param(
    [string]$Python = 'python',
    [string]$DotNet = 'dotnet',
    [string]$NugetSource = 'https://api.nuget.org/v3/index.json',
    [switch]$RebuildBrowser
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& $Python -c "import sys,struct; assert sys.version_info[:2] == (3,10) and struct.calcsize('P') == 4, 'Python 3.10 x86 required'"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 x86 required.' }
& $Python -c "import importlib.metadata as m; from pathlib import Path; pairs=[s.split('==') for s in Path('requirements-lock.txt').read_text().splitlines() if s.strip()]; assert all(m.version(n)==v for n,v in pairs), 'Install requirements-lock.txt into this Python environment'"
if ($LASTEXITCODE -ne 0) { throw 'Install matching requirements-lock.txt dependencies first.' }
if ($RebuildBrowser -or -not (Test-Path -LiteralPath 'third_party/webview_host/XDownloader.WebHost.exe')) {
    & (Join-Path $PSScriptRoot 'webview_host/build.ps1') -DotNet $DotNet -NugetSource $NugetSource
}
if (-not (Test-Path -LiteralPath 'third_party/ffmpeg/ffmpeg.exe')) { throw 'Provide FFmpeg x86; see third_party/ffmpeg/build-win32.sh.' }
if (-not (Test-Path -LiteralPath 'third_party/webview2-runtime/Microsoft.WebView2.FixedVersionRuntime.157.0.4341.60.x86/msedgewebview2.exe')) { throw 'Provide Microsoft WebView2 Fixed Runtime 157.0.4341.60 x86.' }
& $Python -m PyInstaller --noconfirm XDownloader.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Write-Output 'Built dist/XDownloader.exe (Windows x86).'
