# ============================================================
# Loop Engineer - Manual / Remote Trigger (PowerShell)
#
#   .\run_now.ps1                          pick a project from projects.json, run one phase
#   .\run_now.ps1 -Project proj-a          run one phase for proj-a
#   .\run_now.ps1 -Project proj-a -NoPause for remote triggers (Grok / Manus): never waits
#   .\run_now.ps1 -Project proj-a -DryRun  print the agent command instead of running it
#
# The script's exit code is the Python orchestrator's exit code
# (0 ok, 1 error, 2 usage, 3 project already running).
# ============================================================
param(
    [string]$Project,
    [switch]$NoPause,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Orchestrator = Join-Path $PSScriptRoot "loop_orchestrator.py"
$LoopHome = if ($env:LOOP_HOME) { $env:LOOP_HOME } else { $PSScriptRoot }
$ProjectsFile = Join-Path $LoopHome "projects.json"

function Finish([int]$Code) {
    # Keep the window open if launched by double-click (not from a terminal), unless -NoPause.
    if (-not $NoPause -and $Host.Name -eq "ConsoleHost" -and -not $env:WT_SESSION) {
        Read-Host "Press Enter to close" | Out-Null
    }
    exit $Code
}

function Show-State([string]$Label, [string]$StateFile) {
    if (Test-Path -LiteralPath $StateFile) {
        $state = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $color = if ($state.phase -eq "05_done") { "Green" }
                 elseif ($state.phase -eq "06_failed_requires_human") { "Red" }
                 else { "Yellow" }
        Write-Host "  $Label : $($state.phase)  (retries $($state.refactor_retries))" -ForegroundColor $color
    } else {
        Write-Host "  $Label : (no state yet: $StateFile)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Loop Engineer - Trigger (PowerShell)" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] Python not found. Install Python 3 from https://python.org" -ForegroundColor Red
    Finish 1
}

if (-not (Test-Path -LiteralPath $ProjectsFile)) {
    Write-Host "[ERROR] $ProjectsFile not found. Register a project first:" -ForegroundColor Red
    Write-Host "        python `"$Orchestrator`" register <name> <path>" -ForegroundColor Red
    Finish 2
}

$projects = Get-Content -LiteralPath $ProjectsFile -Raw -Encoding UTF8 | ConvertFrom-Json
$names = @($projects.PSObject.Properties | ForEach-Object { $_.Name })
if ($names.Count -eq 0) {
    Write-Host "[ERROR] No projects registered in $ProjectsFile" -ForegroundColor Red
    Finish 2
}

if (-not $Project) {
    if ($NoPause) {
        Write-Host "[ERROR] -NoPause requires -Project <name>. Registered: $($names -join ', ')" -ForegroundColor Red
        Finish 2
    }
    Write-Host "  Registered projects:"
    for ($i = 0; $i -lt $names.Count; $i++) {
        Write-Host ("    [{0}] {1}  ({2})" -f ($i + 1), $names[$i], $projects.($names[$i]).path)
    }
    $choice = Read-Host "  Select project number"
    $n = 0
    if (-not [int]::TryParse($choice, [ref]$n) -or $n -lt 1 -or $n -gt $names.Count) {
        Write-Host "[ERROR] Invalid selection '$choice'" -ForegroundColor Red
        Finish 2
    }
    $Project = $names[$n - 1]
    Write-Host ""
}

if ($names -notcontains $Project) {
    Write-Host "[ERROR] Project '$Project' is not registered. Registered: $($names -join ', ')" -ForegroundColor Red
    Finish 2
}

$StateFile = Join-Path $projects.$Project.path ".loop\state.json"
Write-Host "  Project        : $Project" -ForegroundColor Yellow
Show-State "Current phase " $StateFile
Write-Host ""

$pyArgs = @()
if ($DryRun) { $pyArgs += "--dry-run" }
$pyArgs += @("run", "--project", $Project)

# Native stderr must not become a terminating error (e.g. when a remote caller
# redirects output), and the exit code must come straight from Python.
$ErrorActionPreference = "Continue"
& python $Orchestrator @pyArgs
$rc = $LASTEXITCODE
$ErrorActionPreference = "Stop"

Write-Host ""
Show-State "Next phase    " $StateFile
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Done (exit code $rc). Run again for next phase." -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

Finish $rc
