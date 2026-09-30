<#
  SKEW Batch Report - install / update on a Windows client PC.

  Run from the deployment folder (the one containing docker-compose.yml):
      powershell -ExecutionPolicy Bypass -File deploy\install.ps1

  First run : creates .env (random passwords, this PC's MAC for the licence),
              loads the Docker images and starts the application.
  Later runs: loads any new image file and restarts with it. An existing .env
              is never overwritten (only a missing HOST_MAC is filled in).
#>
# 'Continue': docker writes progress to stderr, which 'Stop' would treat as a
# failure in Windows PowerShell 5.1. Failures are checked via $LASTEXITCODE.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'docker-compose.yml'))) { $root = (Get-Location).Path }
Set-Location $root
$utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# Windows PowerShell 5.1 turns a native command's redirected stderr into a
# terminating error when ErrorActionPreference is Stop - probe with Continue.
function Test-Docker {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    docker info --format '{{.ServerVersion}}' *> $null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $old
    return $ok
}

function Start-DockerIfNeeded {
    if (Test-Docker) { return }
    $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $exe)) { throw 'Docker Desktop is not installed. Install it from https://www.docker.com/products/docker-desktop and run this script again.' }
    Write-Host '    Docker Desktop is not running - starting it (can take 1-2 minutes)...'
    Start-Process $exe
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Docker) { Write-Host '    Docker is ready.'; return }
    }
    throw 'Docker Desktop did not start within 3 minutes. Open it manually, wait until it says "Engine running", then run this script again.'
}

function New-Secret([int]$bytes = 24) {
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    return ($buf | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Get-HostMac {
    # Wired physical adapter first (stable), then any physical adapter.
    $skip = 'Virtual|Hyper-V|VPN|TAP|Bluetooth|Wi-?Fi|Wireless|WAN Miniport|Loopback'
    $nics = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Sort-Object ifIndex
    $nic = $nics | Where-Object { $_.MediaType -eq '802.3' -and $_.InterfaceDescription -notmatch $skip } |
           Select-Object -First 1
    if (-not $nic) { $nic = $nics | Select-Object -First 1 }
    if (-not $nic) { throw 'No physical network adapter found - set HOST_MAC in .env manually.' }
    Write-Host "    Licence bound to adapter: $($nic.Name) ($($nic.InterfaceDescription))"
    return $nic.MacAddress.ToLower().Replace('-', ':')
}

# --- 1. Docker available? -------------------------------------------------------
Write-Step 'Checking Docker'
Start-DockerIfNeeded

# --- 2. .env ----------------------------------------------------------------------
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    Write-Step 'Creating .env'
    $text = Get-Content (Join-Path $root '.env.example') -Raw
    $text = $text -replace '(?m)^DB_PASSWORD=.*$', ('DB_PASSWORD=' + (New-Secret 18))
    $text = $text -replace '(?m)^SECRET_KEY=.*$',  ('SECRET_KEY=' + (New-Secret 32))
    $text = $text -replace '(?m)^HOST_MAC=.*$',    ('HOST_MAC=' + (Get-HostMac))
    [System.IO.File]::WriteAllText($envFile, $text, $utf8NoBom)
} else {
    $text = Get-Content $envFile -Raw
    if ($text -notmatch '(?m)^HOST_MAC=\S+') {
        Write-Step 'Adding HOST_MAC to existing .env'
        if ($text -match '(?m)^HOST_MAC=') { $text = $text -replace '(?m)^HOST_MAC=.*$', ('HOST_MAC=' + (Get-HostMac)) }
        else { $text = $text.TrimEnd() + "`r`nHOST_MAC=" + (Get-HostMac) + "`r`n" }
        [System.IO.File]::WriteAllText($envFile, $text, $utf8NoBom)
    } else {
        Write-Step 'Keeping existing .env'
    }
}

# --- 3. Load images (offline install) -------------------------------------------
$images = Get-ChildItem $root -Filter '*images*.tar*' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($images) {
    Write-Step "Loading Docker images from $($images.Name) (takes a minute)"
    docker load -i $images.FullName
    if ($LASTEXITCODE -ne 0) { throw 'docker load failed' }
    if ($images.Name -match 'batch-report-(\d+(\.\d+)*)') {
        $version = $Matches[1]
        $text = Get-Content $envFile -Raw
        $text = $text -replace '(?m)^APP_VERSION=.*$', "APP_VERSION=$version"
        [System.IO.File]::WriteAllText($envFile, $text, $utf8NoBom)
        Write-Host "    APP_VERSION set to $version"
    }
}

# --- 4. Start -----------------------------------------------------------------------
Write-Step 'Starting SKEW'
docker compose up -d
if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed' }

Write-Step 'Waiting for the application'
$healthy = $false
for ($i = 0; $i -lt 40; $i++) {
    $ErrorActionPreference = 'Continue'
    $state = docker inspect -f '{{.State.Health.Status}}' batch_report_web 2>$null
    $ErrorActionPreference = 'Stop'
    if ($state -eq 'healthy') { $healthy = $true; break }
    Start-Sleep -Seconds 3
}

$port = ((Get-Content $envFile) | Where-Object { $_ -match '^WEB_PORT=' }) -replace '^WEB_PORT=', ''
if (-not $port) { $port = '5000' }
$mac = ((Get-Content $envFile) | Where-Object { $_ -match '^HOST_MAC=' }) -replace '^HOST_MAC=', ''

Write-Host ''
if ($healthy) { Write-Host "SKEW is running:  http://localhost:$port" -ForegroundColor Green }
else { Write-Host 'SKEW did not report healthy yet - check: docker compose logs web' -ForegroundColor Yellow }
Write-Host "Machine ID for the licence key:  $mac" -ForegroundColor Green
Write-Host 'Send the Machine ID to Prolite Automation, then paste the key on the activation screen.'
