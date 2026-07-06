param(
  [int]$AdminPort = 6185,
  [int]$OneBotPort = 6199,
  [switch]$NoOpenBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$AdminPortWasPassed = $PSBoundParameters.ContainsKey("AdminPort")
$OneBotPortWasPassed = $PSBoundParameters.ContainsKey("OneBotPort")

function Use-LocalPortConfig {
  $ConfigPath = Join-Path $ProjectRoot "config.local.json"
  $ExamplePath = Join-Path $ProjectRoot "config.example.json"
  if (-not (Test-Path $ConfigPath) -and (Test-Path $ExamplePath)) {
    Copy-Item -LiteralPath $ExamplePath -Destination $ConfigPath
    Write-Host "Created local config: $ConfigPath"
  }

  if (-not (Test-Path $ConfigPath)) {
    return
  }

  try {
    $Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $script:AdminPortWasPassed -and $Config.'admin.listen_port') {
      $script:AdminPort = [int]$Config.'admin.listen_port'
    }
    if (-not $script:OneBotPortWasPassed -and $Config.'onebot.listen_port') {
      $script:OneBotPort = [int]$Config.'onebot.listen_port'
    }
  } catch {
    Write-Host "Local config exists but port values could not be read, using command defaults."
  }
}

# Some Windows hosts expose both Path and PATH in the process environment.
# Start-Process can crash while copying that duplicated dictionary.
function Repair-ProcessPath {
  $PathValue = [Environment]::GetEnvironmentVariable("Path", "Process")
  if (-not $PathValue) {
    $PathValue = [Environment]::GetEnvironmentVariable("PATH", "Process")
  }
  [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
  if ($PathValue) {
    [Environment]::SetEnvironmentVariable("Path", $PathValue, "Process")
  }
}

function New-ProjectVenv {
  param(
    [string]$VenvDir
  )

  $PyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
  if ($PyLauncher) {
    & $PyLauncher.Source -3 -m venv $VenvDir
  } else {
    $PythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
      throw "Python is not installed or not in PATH. Please install Python 3.11+ first."
    }
    & $PythonCommand.Source -m venv $VenvDir
  }

  if ($LASTEXITCODE -ne 0) {
    throw "Failed to create virtual environment."
  }
}

Use-LocalPortConfig
Repair-ProcessPath

$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
  Write-Host "Creating Python virtual environment..."
  New-ProjectVenv -VenvDir $VenvDir
}

if (-not (Test-Path $VenvPython)) {
  throw "Virtual environment Python was not found: $VenvPython"
}

$Requirements = Join-Path $ProjectRoot "requirements.txt"
if (Test-Path $Requirements) {
  Write-Host "Installing dependencies..."
  & $VenvPython -m pip install -r $Requirements
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to install dependencies."
  }
}

$Python = $VenvPython

function Start-QQbotServer {
  param(
    [string]$Name,
    [int]$Port
  )

  $Args = @("-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "$Port")
  $Process = Start-Process -FilePath $Python -ArgumentList $Args -WorkingDirectory $ProjectRoot -NoNewWindow -PassThru
  Write-Host "$Name starting on port $Port pid=$($Process.Id)"
  return $Process
}

function Wait-AdminReady {
  param(
    [int]$Port,
    [int]$TimeoutSeconds = 25
  )

  $Url = "http://127.0.0.1:$Port/api/status"
  $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $Deadline) {
    try {
      $Response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
      if ($Response.StatusCode -eq 200) {
        return
      }
    } catch {
      Start-Sleep -Milliseconds 500
    }
  }
  throw "Admin service did not become ready: $Url"
}

function Wait-PortReady {
  param(
    [int]$Port,
    [int]$TimeoutSeconds = 25
  )

  $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $Deadline) {
    $Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($Listener) {
      return
    }
    Start-Sleep -Milliseconds 500
  }
  throw "Service port did not become ready: $Port"
}

function Open-AdminWeb {
  param(
    [int]$Port
  )

  $Url = "http://127.0.0.1:$Port/"
  try {
    Write-Host "Opening local web: $Url"
    Start-Process -FilePath $Url
  } catch {
    Write-Host "Could not open browser automatically. Please open this URL manually:"
    Write-Host $Url
  }
}

$Processes = @()
try {
  $Processes += Start-QQbotServer -Name "Admin" -Port $AdminPort

  if ($OneBotPort -ne $AdminPort) {
    $Processes += Start-QQbotServer -Name "OneBot" -Port $OneBotPort
  }

  Wait-AdminReady -Port $AdminPort
  Wait-PortReady -Port $OneBotPort

  Write-Host ""
  Write-Host "QQbot_v2 is ready."
  Write-Host "Local web: http://127.0.0.1:$AdminPort/"
  Write-Host "NapCat reverse websocket: ws://localhost:$OneBotPort/onebot/ws"
  Write-Host "Press Ctrl+C to stop QQbot_v2."

  if (-not $NoOpenBrowser) {
    Open-AdminWeb -Port $AdminPort
  }

  Wait-Process -Id ($Processes | Select-Object -ExpandProperty Id)
} finally {
  foreach ($Process in $Processes) {
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
  }
}
