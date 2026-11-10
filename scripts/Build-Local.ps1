param(
    [string]$Configuration = 'Release',
    [string]$OutputDirectory = '',
    [switch]$UseSourceConfiguration
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$project = Join-Path $root 'src\StreamDownloader.McpHost\StreamDownloader.McpHost.csproj'
$output = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $root 'artifacts\local' }
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$configurationPath = Join-Path $output 'appsettings.json'
$previousConfiguration = if ((-not $UseSourceConfiguration) -and (Test-Path -LiteralPath $configurationPath)) { [IO.File]::ReadAllBytes($configurationPath) } else { $null }
$lockFile = Join-Path (Split-Path -Parent $project) 'packages.lock.json'

if (Test-Path -LiteralPath $lockFile) {
    dotnet restore $project --locked-mode
} else {
    # First build only: create the lock file that every later build restores against.
    dotnet restore $project
}
if ($LASTEXITCODE -ne 0) { throw 'dotnet restore failed.' }

dotnet publish $project `
    --configuration $Configuration `
    --runtime win-x64 `
    --self-contained true `
    --no-restore `
    --output $output `
    -p:PublishSingleFile=true `
    -p:PublishTrimmed=false
if ($LASTEXITCODE -ne 0) { throw 'Local executable build failed.' }
if ($null -ne $previousConfiguration) { [IO.File]::WriteAllBytes($configurationPath, $previousConfiguration) }

$exe = Join-Path $output 'StreamDownloader.McpHost.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Expected executable was not created: $exe"
}

$manifest = [ordered]@{
    version = [Diagnostics.FileVersionInfo]::GetVersionInfo($exe).ProductVersion
    builtAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
    sdk = (dotnet --version)
    executable = $exe
    sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    bytes = (Get-Item -LiteralPath $exe).Length
    toolCount = 6
    localOnly = $true
    validation = 'Build only; see timestamped test-artifacts results for test evidence.'
    configurationSha256 = (Get-FileHash -LiteralPath $configurationPath -Algorithm SHA256).Hash
    sourceFiles = @()
}
$managed = @(Get-ChildItem -LiteralPath $root -File -Force | Where-Object { $_.Name -notlike '*.log' })
foreach ($name in @('src', 'scripts', 'tests', 'tools')) {
    $folder = Join-Path $root $name
    if (Test-Path -LiteralPath $folder) {
        $managed += @(Get-ChildItem -LiteralPath $folder -File -Recurse -Force |
            Where-Object { $_.FullName -notmatch '\\(bin|obj|__pycache__)\\' })
    }
}
$manifest.sourceFiles = @($managed | ForEach-Object {
    @{ path=$_.FullName.Substring($root.Length+1); sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
})
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $output 'manifest.json') -Encoding utf8

"LOCAL BUILD: PASS"
"Executable: $exe"
"SHA256: $((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash)"
