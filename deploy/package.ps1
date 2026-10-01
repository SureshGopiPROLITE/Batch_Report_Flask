<#
  Build a client release package (run on the DEVELOPMENT PC).

      powershell -ExecutionPolicy Bypass -File deploy\package.ps1 -Version 1.0.0

  Produces  release\SKEW-<version>.zip  containing everything the client PC needs:
      batch-report-<version>-images.tar   (app + PostgreSQL images, offline)
      docker-compose.yml, .env.example, DEPLOY.md
      deploy\install.ps1, deploy\db-init\*.sql
  Never included: tools\ (licence generator), your private key, .env, source code.
#>
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [switch]$SkipSeed        # don't regenerate deploy\db-init from the dev database
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if ($Version -notmatch '^\d+(\.\d+)*$') { throw "Version must look like 1.0.0" }

docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop first.' }

if (-not $SkipSeed) {
    Write-Step 'Regenerating database first-run scripts from the dev database'
    python deploy\make_db_init.py
    if ($LASTEXITCODE -ne 0) { throw 'make_db_init.py failed (is the dev database running?)' }
}

Write-Step "Building image batch-report:$Version"
docker build -t "batch-report:$Version" .
if ($LASTEXITCODE -ne 0) { throw 'docker build failed' }

Write-Step 'Pulling PostgreSQL image'
docker pull postgres:18-bookworm
if ($LASTEXITCODE -ne 0) { throw 'docker pull failed (internet needed on the dev PC)' }

$out = Join-Path $root "release\SKEW-$Version"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force (Join-Path $out 'deploy\db-init') | Out-Null

Write-Step 'Saving images (a few minutes)'
docker save "batch-report:$Version" postgres:18-bookworm -o (Join-Path $out "batch-report-$Version-images.tar")
if ($LASTEXITCODE -ne 0) { throw 'docker save failed' }

Write-Step 'Copying deployment files'
Copy-Item docker-compose.yml, .env.example, DEPLOY.md $out
Copy-Item deploy\install.ps1, deploy\fix_db_password.ps1 (Join-Path $out 'deploy')
Copy-Item deploy\db-init\*.sql (Join-Path $out 'deploy\db-init')

Write-Step 'Creating zip'
$zip = "$out.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path "$out\*" -DestinationPath $zip -CompressionLevel Optimal

$size = [math]::Round((Get-Item $zip).Length / 1MB)
Write-Host ''
Write-Host "Release ready: $zip  ($size MB)" -ForegroundColor Green
Write-Host 'Copy it to the client PC, unzip to C:\SKEW, then run deploy\install.ps1 there.'
