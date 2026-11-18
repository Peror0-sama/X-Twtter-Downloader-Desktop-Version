param([string]$DotNet = 'dotnet', [string]$NugetSource = 'https://api.nuget.org/v3/index.json')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
& $DotNet publish (Join-Path $PSScriptRoot 'XDownloader.WebHost.csproj') -c Release -r win-x86 --self-contained true --source $NugetSource -o (Join-Path $projectRoot 'third_party/webview_host')
if ($LASTEXITCODE -ne 0) { throw 'WebView2 x86 host build failed.' }
