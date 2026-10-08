<#
  Opens SKEW in the browser once it is running - for Windows sign-in.

  Docker Desktop starts the containers by itself (restart: unless-stopped),
  but that takes a minute or two after sign-in. This waits until the web app
  answers, then opens it, so the operator never sees "site can't be reached".

  Set up once (see DEPLOY.md "Start SKEW automatically"):
      powershell -ExecutionPolicy Bypass -File deploy\open_skew.ps1 -Install
  which puts a shortcut to this script in the user's Startup folder.

  Options:
      -Kiosk      full-screen browser window without address bar (Edge)
      -Install    create the Startup shortcut (add -Kiosk to make it kiosk)
      -Uninstall  remove the Startup shortcut
#>
param(
    [switch]$Kiosk,
    [switch]$Install,
    [switch]$Uninstall,
    [int]$TimeoutMinutes = 10
)

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'docker-compose.yml'))) { $root = (Get-Location).Path }
$shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'SKEW.lnk'

if ($Uninstall) {
    Remove-Item $shortcut -ErrorAction SilentlyContinue
    Write-Host "Startup shortcut removed."
    return
}

if ($Install) {
    $arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($Kiosk) { $arguments += " -Kiosk" }
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($shortcut)
    $lnk.TargetPath = (Get-Command powershell.exe).Source
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $root
    $lnk.WindowStyle = 7          # minimised
    $lnk.Description = "Open SKEW when it is ready"
    $lnk.Save()
    Write-Host "Startup shortcut created: $shortcut"
    Write-Host "SKEW will open by itself after every Windows sign-in."
    return
}

# Port from .env (WEB_PORT), default 5000
$port = 5000
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^WEB_PORT=\d+' } | Select-Object -First 1
    if ($line) { $port = [int]($line -replace '^WEB_PORT=', '') }
}
$url = "http://localhost:$port/"

# Wait until the web app answers (Docker Desktop + containers starting)
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri ($url + 'plc_status') -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
}

if ($Kiosk) {
    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
              "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($edge) {
        Start-Process $edge -ArgumentList "--kiosk $url --edge-kiosk-type=fullscreen --no-first-run"
        return
    }
}
# Default browser. If SKEW never answered, open it anyway - the page then
# shows the browser's own error, which is easier to report than nothing.
Start-Process $url
