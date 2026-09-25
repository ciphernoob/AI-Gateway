$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPrevious = Get-Location
$taskOldGOOS = $env:GOOS
$taskOldCGO = $env:CGO_ENABLED
try {
    Set-Location (Join-Path $taskRoot 'admin/web')
    npm.cmd ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Vue build failed' }
    Set-Location (Join-Path $taskRoot 'admin')
    $env:GOPATH = Join-Path $taskRoot 'admin/.cache/go'
    $env:GOCACHE = Join-Path $taskRoot 'admin/.cache/go-build'
    New-Item -ItemType Directory -Force (Join-Path $taskRoot '.build') | Out-Null
    $env:CGO_ENABLED = '0'
    $env:GOOS = 'windows'
    go build -trimpath -o ../.build/gateway-admin.exe .
    if ($LASTEXITCODE -ne 0) { throw 'Windows Go build failed' }
    $env:GOOS = 'linux'
    go build -trimpath -o ../.build/gateway-admin .
    if ($LASTEXITCODE -ne 0) { throw 'Linux Go build failed' }
} finally {
    $env:GOOS = $taskOldGOOS
    $env:CGO_ENABLED = $taskOldCGO
    Set-Location $taskPrevious
}
