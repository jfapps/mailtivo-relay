#Requires -Version 5.1
<#
.SYNOPSIS
    Run the full Mailtivo-Relay stack locally, the production way.

.DESCRIPTION
    Brings up the *production* compose stack (docker-compose.yml): gunicorn +
    Postgres 16 + a Django-Q2 worker, all on settings.prod. This is NOT the dev
    overlay — there is no source mount and no auto-reload, so it behaves exactly
    like a deployed instance.

    On first run it bootstraps .env (copies .env.example) and mints the two
    secrets prod refuses to run safely without: DJANGO_SECRET_KEY and
    RELAY_FERNET_KEY.

.PARAMETER Command
    up        (default) bootstrap env, build images, start the stack, wait healthy
    down      stop and remove containers (volumes/data preserved)
    reset     down + delete volumes (DESTROYS the database and stored bodies)
    restart   restart the running services
    logs      follow logs from all services
    ps        show service status
    shell     open a Django shell inside the web container
    migrate   run database migrations and exit
    build     rebuild images without starting
    help      show this message

.EXAMPLE
    .\scripts\prod-local.ps1
    .\scripts\prod-local.ps1 logs
    .\scripts\prod-local.ps1 reset
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = 'up'
)

$ErrorActionPreference = 'Stop'

# Resolve repo root (parent of this script's dir) and work from there.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir   = Split-Path -Parent $ScriptDir
Set-Location $RootDir

$EnvFile        = Join-Path $RootDir '.env'
$EnvExample     = Join-Path $RootDir '.env.example'
$WebPortDefault = 8000

# ── pretty output ───────────────────────────────────────────────────────────
function Write-Info { param([string]$m) Write-Host "[prod-local] $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "[prod-local] $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "[prod-local] $m" -ForegroundColor Yellow }
function Die        { param([string]$m) Write-Host "[prod-local] $m" -ForegroundColor Red; exit 1 }

# ── docker compose detection ──────────────────────────────────────────────────
$script:ComposeCmd  = $null
$script:ComposeArgs = @()

# Run a native command, swallow ALL output, return $true on exit code 0.
# Windows PowerShell 5.1 wraps a native command's stderr in ErrorRecords when
# it is redirected, which becomes a *terminating* error under EAP=Stop — even
# for harmless WARNING lines (e.g. docker's "No blkio throttle" notice). We
# relax EAP for the duration of the call so only the real exit code matters.
function Test-NativeOk {
    param([string]$Exe, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        & $Exe @Arguments 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Resolve-Compose {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Die 'Docker is not installed or not on PATH.'
    }
    if (-not (Test-NativeOk 'docker' @('info'))) {
        Die 'Docker daemon is not running. Start Docker Desktop and retry.'
    }

    if (Test-NativeOk 'docker' @('compose', 'version')) {
        $script:ComposeCmd  = 'docker'
        $script:ComposeArgs = @('compose')
        return
    }
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
        $script:ComposeCmd  = 'docker-compose'
        $script:ComposeArgs = @()
        return
    }
    Die 'Docker Compose not found. Install Docker Desktop / the compose plugin.'
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    # Relax EAP so docker's stderr warnings (e.g. "No blkio throttle") don't get
    # promoted to terminating errors. Output still streams live to the console.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $script:ComposeCmd @($script:ComposeArgs + $Args)
    } finally {
        $ErrorActionPreference = $prev
    }
}

# ── secret generation ─────────────────────────────────────────────────────────
function Get-RandomBytes {
    param([int]$Count)
    $bytes = New-Object byte[] $Count
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return $bytes
}

# Fernet key = URL-safe base64 of 32 random bytes (44 chars, trailing '=').
function New-FernetKey {
    $b64 = [Convert]::ToBase64String((Get-RandomBytes 32))
    return $b64.Replace('+', '-').Replace('/', '_')
}

# Django secret key = any long, random, opaque string.
function New-SecretKey {
    $b64 = [Convert]::ToBase64String((Get-RandomBytes 48))
    return $b64.Replace('+', '-').Replace('/', '_').Replace('=', '')
}

function Get-EnvVar {
    param([string]$Key)
    if (-not (Test-Path $EnvFile)) { return '' }
    $line = Select-String -Path $EnvFile -Pattern "^$([regex]::Escape($Key))=" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $line) { return '' }
    return ($line.Line -replace "^$([regex]::Escape($Key))=", '')
}

# Write lines as UTF-8 WITHOUT a BOM. Windows PowerShell 5.1's
# `Set-Content -Encoding utf8` emits a BOM, and docker's env-file parser reads
# the leading ﻿ as part of the first variable name ("unexpected character").
function Write-LinesNoBom {
    param([string]$Path, [string[]]$Lines)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $Lines, $utf8NoBom)
}

function Set-EnvVar {
    param([string]$Key, [string]$Value)
    # Read as UTF-8 (5.1's default reader is ANSI and mangles non-ASCII).
    $lines = @(Get-Content -Path $EnvFile -Encoding UTF8)
    $found = $false
    $out = foreach ($l in $lines) {
        if ($l -match "^$([regex]::Escape($Key))=") { $found = $true; "$Key=$Value" }
        else { $l }
    }
    if (-not $found) { $out = $out + "$Key=$Value" }
    Write-LinesNoBom -Path $EnvFile -Lines $out
}

function Test-Placeholder {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    foreach ($p in 'change-me', 'dev-only', 'dev-insecure', 'replace-me') {
        if ($Value -like "*$p*") { return $true }
    }
    return $false
}

function Initialize-Env {
    if (-not (Test-Path $EnvFile)) {
        if (-not (Test-Path $EnvExample)) { Die 'Neither .env nor .env.example found.' }
        Copy-Item $EnvExample $EnvFile
        Write-Info 'Created .env from .env.example'
    }

    if (Test-Placeholder (Get-EnvVar 'DJANGO_SECRET_KEY')) {
        Set-EnvVar 'DJANGO_SECRET_KEY' (New-SecretKey)
        Write-Ok 'Minted a fresh DJANGO_SECRET_KEY'
    }
    if (Test-Placeholder (Get-EnvVar 'RELAY_FERNET_KEY')) {
        Set-EnvVar 'RELAY_FERNET_KEY' (New-FernetKey)
        Write-Ok 'Minted a fresh RELAY_FERNET_KEY (body encryption)'
    }

    # Local prod-over-HTTP: prod.py defaults SSL redirect on. Keep the stack
    # reachable on plain http://localhost without an HTTPS proxy.
    if (Test-Placeholder (Get-EnvVar 'SECURE_SSL_REDIRECT')) { Set-EnvVar 'SECURE_SSL_REDIRECT' 'false' }
    if (Test-Placeholder (Get-EnvVar 'SECURE_HSTS_SECONDS'))  { Set-EnvVar 'SECURE_HSTS_SECONDS' '0' }
}

function Get-WebPort {
    $p = Get-EnvVar 'WEB_PORT'
    if ([string]::IsNullOrWhiteSpace($p)) { return $WebPortDefault }
    return $p
}

# ── wait for the web healthcheck ─────────────────────────────────────────────
function Wait-Healthy {
    $port = Get-WebPort
    Write-Info 'Waiting for the web service to become healthy…'
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:$port/healthz/" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { Write-Ok 'Web is healthy.'; return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    Write-Warn "Web did not report healthy after 120s. Check: .\scripts\prod-local.ps1 logs"
    return $false
}

function Show-NextSteps {
    $port = Get-WebPort
    Write-Ok 'Stack is up — running exactly like production (gunicorn + Postgres + Q2 worker).'
    Write-Host ''
    Write-Host "  Panel / onboarding : http://localhost:$port/"
    Write-Host "  API base URL       : http://localhost:$port/api/v1"
    Write-Host "  Health             : http://localhost:$port/healthz/"
    Write-Host ''
    Write-Host '  Logs   : .\scripts\prod-local.ps1 logs'
    Write-Host '  Stop   : .\scripts\prod-local.ps1 down'
    Write-Host '  Wipe   : .\scripts\prod-local.ps1 reset   (deletes the database!)'
    Write-Host ''
    Write-Warn 'Heads-up: settings.prod sets SESSION_COOKIE_SECURE / CSRF_COOKIE_SECURE = True.'
    Write-Warn 'The API works fine over http, but browser panel LOGIN needs HTTPS to keep a'
    Write-Warn 'session. Put a TLS proxy in front, or use the dev overlay for browser work:'
    Write-Warn '  docker compose -f docker-compose.yml -f docker-compose.dev.yml up'
}

# ── commands ─────────────────────────────────────────────────────────────────
function Command-Up {
    Resolve-Compose
    Initialize-Env
    Write-Info 'Building images…'
    Invoke-Compose up -d --build
    [void](Wait-Healthy)
    Show-NextSteps
}

function Command-Down    { Resolve-Compose; Invoke-Compose down; Write-Ok 'Stack stopped (data preserved).' }
function Command-Reset {
    Resolve-Compose
    Write-Warn 'This deletes Postgres data, stored message bodies, and static volumes.'
    Invoke-Compose down -v
    Write-Ok 'Stack and volumes removed.'
}
function Command-Restart { Resolve-Compose; Invoke-Compose restart; Write-Ok 'Services restarted.' }
function Command-Logs    { Resolve-Compose; Invoke-Compose logs -f --tail=200 }
function Command-Ps      { Resolve-Compose; Invoke-Compose ps }
function Command-Build   { Resolve-Compose; Initialize-Env; Invoke-Compose build }
function Command-Shell   { Resolve-Compose; Invoke-Compose exec web python manage.py shell }
function Command-Migrate { Resolve-Compose; Invoke-Compose run --rm web migrate }
function Command-Help    { Get-Help $MyInvocation.MyCommand.Path -Detailed }

switch ($Command.ToLower()) {
    'up'      { Command-Up }
    'down'    { Command-Down }
    'reset'   { Command-Reset }
    'restart' { Command-Restart }
    'logs'    { Command-Logs }
    'ps'      { Command-Ps }
    'status'  { Command-Ps }
    'build'   { Command-Build }
    'shell'   { Command-Shell }
    'migrate' { Command-Migrate }
    'help'    { Command-Help }
    default   { Die "Unknown command '$Command'. Run '.\scripts\prod-local.ps1 help'." }
}
