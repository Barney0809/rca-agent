# ============================================================
#  rca-agent environment doctor  (READ-ONLY, changes nothing)
#
#  Why this script exists:
#    1. Lets anyone who clones the repo confirm the environment
#       meets requirements with a single command (reproducibility).
#    2. Turns "the dependencies actually import" into something
#       verifiable instead of merely declared.
#
#  Usage:
#    powershell -NoProfile -File scripts/dev.ps1
#    or  make doctor
#
#  ===== ENCODING RULE - DO NOT BREAK =====
#  This file MUST stay pure ASCII.
#  Windows PowerShell 5.1 reads a UTF-8-without-BOM script as ANSI/GBK,
#  which mangles any non-ASCII text and can corrupt the parser itself.
#  That exact failure mode caused a real, unrecoverable data-loss
#  incident on 2026-09-22. Keep scripts ASCII; use .md for Chinese.
#  ============================================================
# ============================================================

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$fail = 0
function Ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:fail++ }
function Head($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

Head "1. Python interpreter"
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    $v = & $venvPy -c "import sys;print(sys.version.split()[0])"
    Ok "project venv: $venvPy  (python $v)"
} else {
    Bad "no .venv found - run: uv sync"
}

# Cross-check what a bare `python` resolves to.
#
# WHY THIS CHECK EXISTS (see docs/harness-log.md P1):
#   Running a script with the system `python` instead of the project venv
#   does NOT fail loudly - it runs fine and reports a completely wrong fact,
#   e.g. "ModuleNotFoundError: langgraph" for a package that IS installed.
#   That looks like a broken environment when it is actually a broken command.
#
#   So: make the mismatch visible here, in the first lines of the self-check.
if (Test-Path $venvPy) {
    $venvPrefix = Split-Path -Parent (Split-Path -Parent $venvPy)
    $bare = Get-Command python -ErrorAction SilentlyContinue
    if ($bare) {
        $barePrefix = (& python -c "import sys;print(sys.prefix)" 2>$null)
        if ($barePrefix -and $barePrefix.Trim() -ne $venvPrefix) {
            Warn "bare 'python' resolves to $($bare.Source)"
            Warn "  its prefix is $barePrefix"
            Warn "  that is NOT the project venv - results from it can be misleading"
            Warn "  always use: .\.venv\Scripts\python.exe   or: uv run python"
        } else {
            Ok "bare 'python' points into the project venv"
        }
    } else {
        Warn "bare 'python' not found on PATH"
    }
}

Head "2. Dependency importability (the real compatibility test)"
if (Test-Path $venvPy) {
    $code = @'
import importlib
mods = ["langgraph","langchain_core","langchain_openai","mcp","pydantic",
        "fastapi","uvicorn","httpx","tiktoken","structlog","openai","sse_starlette"]
bad = 0
for m in mods:
    try:
        importlib.import_module(m)
        print("  [OK]   " + m)
    except Exception as e:
        print("  [FAIL] " + m + " -> " + type(e).__name__ + ": " + str(e))
        bad += 1
print("IMPORT_FAILURES=" + str(bad))
'@
    $out = $code | & $venvPy -
    $out | Where-Object { $_ -notmatch '^IMPORT_FAILURES=' } | ForEach-Object { Write-Host $_ }
    $failures = 0
    foreach ($line in $out) {
        if ($line -match '^IMPORT_FAILURES=(\d+)') { $failures = [int]$Matches[1] }
    }
    if ($failures -gt 0) { Bad "$failures dependency/dependencies failed to import" }
}

Head "3. LangGraph API (avoid deprecated paths from old tutorials)"
if (Test-Path $venvPy) {
    $code2 = @'
try:
    from langgraph.types import Send
    print("  [OK]   from langgraph.types import Send")
except Exception as e:
    print("  [FAIL] langgraph.types.Send -> " + str(e))
try:
    from langgraph.graph import StateGraph
    print("  [OK]   from langgraph.graph import StateGraph")
except Exception as e:
    print("  [FAIL] StateGraph -> " + str(e))
'@
    $code2 | & $venvPy - | ForEach-Object { Write-Host $_ }
}

Head "4. Docker"
$d = Get-Command docker -ErrorAction SilentlyContinue
if ($d) {
    $dv = (docker version --format '{{.Server.Version}}' 2>$null)
    if ($dv) { Ok "Docker running, server $dv" } else { Bad "Docker installed but daemon not running" }
} else { Bad "Docker not installed" }

Head "5. Credentials and network"
if ($env:DEEPSEEK_API_KEY) { Ok "DEEPSEEK_API_KEY set (len=$($env:DEEPSEEK_API_KEY.Length))" }
else { Warn "DEEPSEEK_API_KEY not set (offline replay still works)" }

foreach ($u in @('https://api.deepseek.com','https://pypi.org')) {
    try {
        Invoke-WebRequest -Uri $u -UseBasicParsing -Method Head -TimeoutSec 15 -ErrorAction Stop | Out-Null
        Ok "reachable $u"
    } catch {
        $c = $_.Exception.Response.StatusCode.value__
        if ($c) { Ok "reachable $u (HTTP $c)" } else { Warn "unreachable $u" }
    }
}

Head "6. Safety self-check"
$gitignore = Join-Path $root '.gitignore'
if (Test-Path $gitignore) {
    Push-Location $root
    foreach ($p in @('.env','secrets.env','quarantine/x')) {
        git check-ignore -q $p
        if ($LASTEXITCODE -eq 0) { Ok "ignored: $p" } else { Bad "NOT ignored: $p -- credential leak risk" }
    }
    Pop-Location
} else { Bad ".gitignore missing" }

Head "Verdict"
if ($fail -eq 0) { Write-Host "  environment ready" -ForegroundColor Green }
else { Write-Host "  $fail item(s) need attention" -ForegroundColor Red }
exit $fail
