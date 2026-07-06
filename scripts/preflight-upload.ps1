param(
  [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

$Failures = New-Object System.Collections.Generic.List[string]

function Add-Failure {
  param([string]$Message)
  $Failures.Add($Message) | Out-Null
}

function Test-RequiredFile {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $Path))) {
    Add-Failure "Missing required file: $Path"
  }
}

function Test-JsonFile {
  param([string]$Path)
  $FullPath = Join-Path $ProjectRoot $Path
  try {
    Get-Content -LiteralPath $FullPath -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
  } catch {
    Add-Failure "Invalid JSON: $Path"
  }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Add-Failure "git command not found."
} else {
  $Status = git status --short
  if ($Status -and -not $AllowDirty) {
    Add-Failure "Working tree is dirty. Commit changes first."
  }

  $Tracked = git ls-files
  foreach ($File in $Tracked) {
    $Normalized = $File -replace "\\", "/"
    if (
      $Normalized -eq "config.local.json" -or
      $Normalized -eq "data/bot.sqlite3" -or
      $Normalized.StartsWith(".venv/") -or
      $Normalized.StartsWith("data/memories/") -or
      $Normalized.StartsWith("logs/") -or
      $Normalized.StartsWith("backups/") -or
      $Normalized.EndsWith(".lnk")
    ) {
      Add-Failure "Sensitive or local runtime file is tracked by Git: $File"
    }
  }

  $ExpectedIgnored = @(
    "config.local.json",
    ".venv/test.txt",
    "data/bot.sqlite3",
    "data/memories/test.json",
    "logs/test.log",
    "backups/test.zip",
    "shortcut.lnk"
  )
  $Ignored = git check-ignore @ExpectedIgnored 2>$null
  foreach ($Path in $ExpectedIgnored) {
    if ($Ignored -notcontains $Path) {
      Add-Failure ".gitignore does not ignore: $Path"
    }
  }
}

@(
  "README.md",
  ".gitignore",
  "start.ps1",
  "start-qqbot-v2.bat",
  "requirements.txt",
  "config.example.json",
  "data/commands.json",
  "app/main.py",
  "app/onebot.py",
  "app/admin_api.py"
) | ForEach-Object { Test-RequiredFile $_ }

Test-JsonFile "config.example.json"
Test-JsonFile "data/commands.json"

if ($Failures.Count -gt 0) {
  Write-Host "Preflight upload check failed:" -ForegroundColor Red
  foreach ($Failure in $Failures) {
    Write-Host "- $Failure" -ForegroundColor Red
  }
  exit 1
}

Write-Host "Preflight upload check passed. QQbot_v2 is ready to upload." -ForegroundColor Green
