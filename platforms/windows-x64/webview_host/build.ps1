param([string]$DependencyDirectory = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $DependencyDirectory) { $DependencyDirectory = Join-Path $projectRoot 'dependencies' }
$bundle = Join-Path $DependencyDirectory 'webview2-build'
$sdk = Join-Path $bundle 'sdk'
if (-not (Test-Path -LiteralPath (Join-Path $sdk 'dotnet.exe'))) {
    Expand-Archive -LiteralPath (Join-Path $bundle 'dotnet-sdk-10.0.404-win-x64.zip') -DestinationPath $sdk
}
$runtime = Join-Path $projectRoot 'third_party\webview2-runtime'
if (-not (Test-Path -LiteralPath (Join-Path $runtime 'Microsoft.WebView2.FixedVersionRuntime.157.0.4341.60.x64\msedgewebview2.exe'))) {
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    & expand.exe (Join-Path $bundle 'webview2-157.0.4341.60-x64.cab') '-F:*' $runtime | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'WebView2 runtime extraction failed.' }
}
$signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $runtime 'Microsoft.WebView2.FixedVersionRuntime.157.0.4341.60.x64\msedgewebview2.exe')
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'WebView2 signature validation failed.' }
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
$dotnet = Join-Path $sdk 'dotnet.exe'
$cache = Join-Path $bundle 'nuget'
$project = Join-Path $PSScriptRoot 'XDownloader.WebHost.csproj'
& $dotnet restore $project --source (Join-Path $bundle 'packages') --packages $cache '-p:SelfContained=true'
if ($LASTEXITCODE -ne 0) { throw 'Offline WebView2 restore failed.' }
& $dotnet publish $project -c Release --no-restore "-p:RestorePackagesPath=$cache" -o (Join-Path $projectRoot 'third_party\webview_host')
if ($LASTEXITCODE -ne 0) { throw 'WebView2 host build failed.' }
