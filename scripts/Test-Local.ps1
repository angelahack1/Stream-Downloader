param(
    [switch]$SkipBuild,
    [switch]$SkipMyVideos,
    [string[]]$Only = @(),
    [string]$MyVideosConfig = ''
)
# One command, one VISIBLE window, every phase in order. Run it in the foreground, e.g.:
#   Start-Process pwsh -ArgumentList '-NoExit','-File','C:\Development\XAIHT\Stream-Downloader\scripts\Test-Local.ps1'
# Phases: build, unit, decoupling, comparison, local, tlamatini, myvideos. -Only picks a subset.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$suite = Join-Path $root ('test-artifacts\suite-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $suite -Force | Out-Null
$suiteLog = Join-Path $suite 'suite.log'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
try { $Host.UI.RawUI.WindowTitle = 'Stream-Downloader - complete visible test suite' } catch { }

function Say([string]$Text, [string]$Color = 'Gray') {
    $line = '{0} {1}' -f (Get-Date -Format 'HH:mm:ss'), $Text
    Write-Host $line -ForegroundColor $Color
    Add-Content -LiteralPath $suiteLog -Value $line -Encoding utf8
}

function Take-Photo([string]$Name) {
    python -u (Join-Path $root 'tests\take_photo.py') --dir (Join-Path $suite 'photos') --name $Name
}

$all = @('build', 'unit', 'decoupling', 'comparison', 'local', 'tlamatini', 'myvideos')
$selected = if ($Only.Count) { $all | Where-Object { $Only -contains $_ } } else { $all }
if ($SkipBuild) { $selected = $selected | Where-Object { $_ -ne 'build' } }
if ($SkipMyVideos) { $selected = $selected | Where-Object { $_ -ne 'myvideos' } }
$summary = [ordered]@{ startedAtUtc = [DateTimeOffset]::UtcNow.ToString('o'); suite = $suite; phases = @(); passed = $false }

Say ('=' * 78) Magenta
Say "  Stream-Downloader - complete visible test suite" White
Say "  Angela, every phase runs right here in this window. Evidence: $suite" Magenta
Say ('=' * 78) Magenta
Push-Location -LiteralPath $root
try {
    foreach ($phase in $selected) {
        $evidence = Join-Path $suite $phase
        $started = Get-Date
        Say "=== PHASE $phase ===" Cyan
        $code = 0
        try {
            switch ($phase) {
                'build' {
                    & (Join-Path $root 'scripts\Build-Local.ps1') *>&1 | Tee-Object -FilePath (Join-Path $suite 'build.log')
                    Take-Photo 'build.png'
                }
                'unit' {
                    dotnet run --project (Join-Path $root 'tests\StreamDownloader.Tests') -c Release -- --output $evidence
                    $code = $LASTEXITCODE
                    Take-Photo 'unit.png'
                }
                'decoupling' { python -u (Join-Path $root 'tests\verify_decoupling.py') --run $evidence; $code = $LASTEXITCODE }
                'comparison' { python -u (Join-Path $root 'tests\verify_comparison.py') --run $evidence; $code = $LASTEXITCODE }
                'local' { python -u (Join-Path $root 'tests\verify_local.py') --run $evidence; $code = $LASTEXITCODE }
                'tlamatini' { python -u (Join-Path $root 'tests\verify_tlamatini_load.py') --run $evidence; $code = $LASTEXITCODE }
                'myvideos' {
                    $arguments = @('-u', (Join-Path $root 'tests\verify_my_videos.py'), '--run', $evidence)
                    if ($MyVideosConfig) { $arguments += @('--config', $MyVideosConfig) }
                    python @arguments
                    $code = $LASTEXITCODE
                }
            }
        }
        catch {
            Say ($_ | Out-String) Red
            $code = 1
        }
        $seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
        $summary.phases += [ordered]@{ phase = $phase; exitCode = $code; seconds = $seconds; evidence = $evidence }
        $color = if ($code -eq 0) { 'Green' } elseif ($code -eq 3) { 'Yellow' } else { 'Red' }
        Say "PHASE $phase -> exit $code in $seconds s" $color
        if ($phase -eq 'build' -and $code -ne 0) { Say 'Build failed: the remaining phases need the executable, stopping.' Red; break }
    }
}
finally {
    Pop-Location
    $failed = @($summary.phases | Where-Object { $_.exitCode -ne 0 -and $_.exitCode -ne 3 })
    $incomplete = @($summary.phases | Where-Object { $_.exitCode -eq 3 })
    $summary.passed = ($failed.Count -eq 0 -and $incomplete.Count -eq 0 -and $summary.phases.Count -eq $selected.Count)
    $summary.finishedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
    $summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $suite 'summary.json') -Encoding utf8
    Say ('=' * 78) Magenta
    foreach ($p in $summary.phases) { Say ("  {0,-12} exit {1,-3} {2,8} s" -f $p.phase, $p.exitCode, $p.seconds) $(if ($p.exitCode -eq 0) { 'Green' } elseif ($p.exitCode -eq 3) { 'Yellow' } else { 'Red' }) }
    $verdict = if ($summary.passed) { 'PASS' } elseif ($failed.Count) { 'FAIL' } else { 'PASS WITH MISSING EVIDENCE' }
    Say "SUITE-STATUS: $verdict ($($summary.phases.Count) phases) - evidence: $suite" $(if ($summary.passed) { 'Green' } else { 'Red' })
    Say ('=' * 78) Magenta
}
if (-not $summary.passed) { exit 1 }
