<#
  Fix "password authentication failed for user postgres".

  Makes all three agree with DB_PASSWORD in .env:
    1. the password stored inside the database (pg_data volume)
    2. the password the running web container was created with
    3. .env itself
  Data is kept. Run from the SKEW folder (the one with docker-compose.yml):

      powershell -ExecutionPolicy Bypass -File deploy\fix_db_password.ps1
      powershell -ExecutionPolicy Bypass -File deploy\fix_db_password.ps1 -Password 12345678
#>
param([string]$Password)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'docker-compose.yml'))) { $root = (Get-Location).Path }
Set-Location $root
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) { throw ".env not found in $root - run this from the SKEW folder" }
$utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Native([scriptblock]$cmd) {          # run docker without PS 5.1 turning stderr into errors
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = & $cmd 2>&1 | ForEach-Object { "$_" }
    $script:rc = $LASTEXITCODE; $ErrorActionPreference = $old
    return ($out -join "`n").Trim()
}
function Get-EnvValue($name, $default = '') {
    $line = Get-Content $envFile | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -Last 1
    if ($line) { return ($line -replace "^\s*$name\s*=", '').Trim().Trim('"').Trim("'") } else { return $default }
}

# --- 1. .env -------------------------------------------------------------------
$lines = @(Get-Content $envFile | Where-Object { $_ -match '^\s*DB_PASSWORD\s*=' })
if ($Password) {
    $text = (Get-Content $envFile | Where-Object { $_ -notmatch '^\s*DB_PASSWORD\s*=' }) -join "`r`n"
    $text = $text.TrimEnd() + "`r`nDB_PASSWORD=$Password`r`n"
    [System.IO.File]::WriteAllText($envFile, $text, $utf8NoBom)
    Step "DB_PASSWORD in .env set to the given password"
} elseif ($lines.Count -gt 1) {
    Write-Host "    WARNING: .env has $($lines.Count) DB_PASSWORD lines - the last one is used" -ForegroundColor Yellow
}
$pass = Get-EnvValue 'DB_PASSWORD'
$user = Get-EnvValue 'DB_USER' 'postgres'
if (-not $pass) { throw 'DB_PASSWORD is empty in .env' }
Step "Using DB_USER=$user and DB_PASSWORD from .env ($($pass.Length) characters)"

# --- 2. containers present, and which folder they belong to ------------------------
foreach ($c in 'batch_report_db', 'batch_report_web') {
    Native { docker inspect $c } | Out-Null
    if ($rc -ne 0) { throw "Container $c not found - start SKEW with deploy\install.ps1 first" }
}
# (labels read as JSON: PowerShell 5.1 strips the inner quotes of a Go template)
$labels = (Native { docker inspect --format '{{json .Config.Labels}}' batch_report_web }) | ConvertFrom-Json
$project = $labels.'com.docker.compose.project'
$workdir = $labels.'com.docker.compose.project.working_dir'
if ($workdir -and ((Resolve-Path $workdir -ErrorAction SilentlyContinue).Path -ne (Resolve-Path $root).Path)) {
    Write-Host "    NOTE: SKEW was started from $workdir, not $root." -ForegroundColor Yellow
    Write-Host "          Using that folder's project '$project' so the right containers are updated." -ForegroundColor Yellow
}

# --- 3. database password ------------------------------------------------------------
Step 'Setting the password inside the database'
$sql = "ALTER ROLE `"$user`" WITH PASSWORD '" + $pass.Replace("'", "''") + "';"
$out = Native { $sql | docker exec -i batch_report_db psql -v ON_ERROR_STOP=1 -q -U $user -d postgres }
if ($rc -ne 0) { throw "Could not set the database password: $out" }

# --- 4. recreate the web container with the .env password -------------------------------
Step 'Recreating the app container with the .env password'
$composeArgs = @('compose', '--env-file', $envFile, '-f', (Join-Path $root 'docker-compose.yml'))
if ($project) { $composeArgs += @('-p', $project) }
$out = Native { docker @composeArgs up -d --no-deps --force-recreate web }
if ($rc -ne 0) { throw "docker compose failed: $out" }

$inContainer = Native { docker exec batch_report_web printenv DB_PASSWORD }
if ($inContainer -ne $pass) { throw "The app container still has a different DB_PASSWORD - check .env in $root" }

# --- 5. prove the app can log in ---------------------------------------------------------
Step 'Testing the app login to the database'
$test = "import os, psycopg2; psycopg2.connect(host='postgres', port=5432, dbname=os.environ.get('DB_NAME','PLCDB2'), user=os.environ['DB_USER'] if os.environ.get('DB_USER') else 'postgres', password=os.environ['DB_PASSWORD']).close(); print('OK')"
$ok = $false
for ($i = 0; $i -lt 15; $i++) {
    $out = Native { docker exec batch_report_web python -c $test }
    if ($out -match 'OK$') { $ok = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ok) { throw "App still cannot log in to the database: $out" }

Write-Host ''
Write-Host 'Fixed: the database, the app and .env all use the same password.' -ForegroundColor Green
Write-Host 'Reload the SKEW page in the browser.'
