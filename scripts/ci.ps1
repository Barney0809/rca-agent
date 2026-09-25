# rca-agent offline gate - the same checks as .github/workflows/ci.yml
#
# WHY THIS FILE EXISTS
#   There is no git remote in the development environment, so GitHub Actions
#   cannot be run from here. This script is the local equivalent: it runs the
#   exact same commands so the pipeline can be verified before it is pushed.
#
# WHAT IT RUNS (all free, no API key, no Docker required)
#   1. ruff static gate      undefined names / async misuse / duplicate defs
#   2. pytest                the whole suite, minus the world integration tests
#                            (those need Docker; they run when the world is up)
#   3. mutate_check --verify-only
#                            fast staleness check of every mutation definition
#   4. seal_report --skip-mutations
#                            every "sealed" claim must have a mutation group
#
# NOTE: This file is deliberately ASCII-only. Non-ASCII inside a parsed script
#       is a known hazard on Windows (see scripts/dev.ps1 header and AGENTS.md
#       rule 1). All user-facing output below is therefore English.
#
# USAGE
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ci.ps1
#   powershell ... -File scripts/ci.ps1 -SkipWorld
#
#   -SkipWorld: never run the world integration tests. Use it when something
#   else is already driving the diagnosed world -- an eval run mutates the same
#   fault knobs, so running both at once corrupts BOTH (a measurement and a
#   test pretending to be independent, harness-log #13 territory).

param([switch]$SkipWorld)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$failures = @()

function Step {
    param([string]$Name, [string[]]$Cmd)
    Write-Host ""
    Write-Host ("=" * 78)
    Write-Host "  $Name"
    Write-Host ("=" * 78)
    & $Cmd[0] $Cmd[1..($Cmd.Length - 1)]
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [FAIL] $Name (exit $LASTEXITCODE)"
        $script:failures += $Name
    } else {
        Write-Host "  [OK]   $Name"
    }
}

# Is the diagnosed world reachable? If yes, include the integration tests.
$worldUp = $false
try {
    $worldUp = (Test-NetConnection -ComputerName 127.0.0.1 -Port 8080 `
                -InformationLevel Quiet -WarningAction SilentlyContinue)
} catch { $worldUp = $false }
if ($SkipWorld) { $worldUp = $false }

Write-Host "rca-agent offline gate"
Write-Host "  python : $py"
Write-Host "  world  : $(if ($SkipWorld) { 'SKIPPED by request (-SkipWorld)' } elseif ($worldUp) { 'UP - integration tests included' } else { 'DOWN - integration tests excluded' })"

Step "ruff (F821/ASYNC/F811)" @($py, "-m", "ruff", "check", "--select", "F821,ASYNC,F811",
                                "src", "eval", "scripts", "tests", "world")

$pytestArgs = @($py, "-m", "pytest", "--color=no", "-p", "no:cacheprovider", "--tb=short")
if (-not $worldUp) { $pytestArgs += "--ignore=tests/test_world.py" }
Step "pytest" $pytestArgs

Step "mutation definitions still apply (fast)" @($py, "scripts\mutate_check.py", "--verify-only")

Step "seal claims have mutation backing (fast)" @($py, "scripts\seal_report.py", "--skip-mutations")

Write-Host ""
Write-Host ("=" * 78)
if ($failures.Count -eq 0) {
    Write-Host "  GATE PASSED"
    exit 0
} else {
    Write-Host "  GATE FAILED: $($failures -join ', ')"
    exit 1
}
