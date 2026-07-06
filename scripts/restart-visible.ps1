$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

foreach ($Port in @(6185, 6199)) {
  Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object {
      Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Milliseconds 800

Start-Process `
  -FilePath "powershell.exe" `
  -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ProjectRoot "start.ps1")) `
  -WorkingDirectory $ProjectRoot `
  -WindowStyle Normal

Write-Host "QQbot_v2 restart command sent."
Write-Host "Local web: http://127.0.0.1:6185/"
