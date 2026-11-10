param([string]$ServerExecutable = (Join-Path (Split-Path -Parent $PSScriptRoot) 'artifacts\local\StreamDownloader.McpHost.exe'))
$ErrorActionPreference = 'Stop'
python -u (Join-Path (Split-Path -Parent $PSScriptRoot) 'tests\verify_local.py') --executable $ServerExecutable --protocol-only
if ($LASTEXITCODE -ne 0) { throw 'MCP protocol verification failed.' }
