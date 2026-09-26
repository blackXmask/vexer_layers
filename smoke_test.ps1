<#
.SYNOPSIS
  Live integration smoke test for the four Vexer domain services.

.DESCRIPTION
  Unit tests prove functions behave; this proves the four services actually boot,
  answer over HTTP, and that the governance machinery fires end to end. It asserts
  nothing about Person A's seams: those are absent by design and must read UNAVAILABLE.

  Exit code 0 = every check passed. Any FAIL is a real defect, not a fixture problem.

.USAGE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\smoke_test.ps1
  Requires: .venv with the project dependencies installed.
#>
$ErrorActionPreference = 'Continue'

# Resolve the repo root from this script's own location so it runs from anywhere.
$base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $base
$py = Join-Path $base '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { Write-Error "no venv interpreter at $py - create it per README.md"; exit 2 }

# Stop leftovers so a rerun is deterministic; free the ports.
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 2

$services = @(
  '8000|agent_orchestrator.api',
  '8001|business_market_intelligence.api',
  '8002|opportunity_risk_intelligence.api',
  '8003|legal_regulatory_ip_intelligence.api'
)

$logDir = Join-Path $env:TEMP 'vexer_smoke'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

foreach ($entry in $services) {
  $port, $module = $entry.Split('|')
  $importString = "${module}:app"
  $cmdArgs = "-m uvicorn $importString --port $port --host 127.0.0.1"
  Start-Process -FilePath $py -ArgumentList $cmdArgs -WindowStyle Hidden -WorkingDirectory $base `
    -RedirectStandardOutput (Join-Path $logDir "port_$port.out") `
    -RedirectStandardError  (Join-Path $logDir "port_$port.err")
}

Write-Output '--- starting 4 services, waiting 18s ---'
Start-Sleep 18

$pass = 0
$fail = 0
function Confirm($name, $ok, $detail) {
  if ($ok) { Write-Host "  PASS  $name  $detail" -ForegroundColor Green; $script:pass++ }
  else     { Write-Host "  FAIL  $name  $detail" -ForegroundColor Red;   $script:fail++ }
}

Write-Output ''
Write-Output '=== 1. Every service boots and answers /health ==='
foreach ($entry in $services) {
  $port = $entry.Split('|')[0]
  try {
    $h = Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 20
    Confirm "port $port /health" $true "($($h.status))"
  } catch {
    Confirm "port $port /health" $false $_.Exception.Message
  }
}

Write-Output ''
Write-Output '=== 2. Domains 7/8/9 serve real payloads ==='
# `topic` is a REQUIRED query param on the D7 GETs; omitting it yields a legitimate 422.
try {
  $r = Invoke-RestMethod 'http://127.0.0.1:8001/intelligence/signals?topic=sovereign-ai&limit=3' -TimeoutSec 20
  Confirm 'D7 /intelligence/signals' ($r.Count -gt 0) "($($r.Count) signals)"
} catch { Confirm 'D7 /intelligence/signals' $false $_.Exception.Message }

foreach ($ep in 'competitors', 'trends', 'segments') {
  try {
    $r = Invoke-RestMethod "http://127.0.0.1:8001/intelligence/$ep`?topic=sovereign-ai" -TimeoutSec 20
    Confirm "D7 /intelligence/$ep" ($null -ne $r) "($($r.Count) items)"
  } catch { Confirm "D7 /intelligence/$ep" $false $_.Exception.Message }
}
try {
  $r = Invoke-RestMethod 'http://127.0.0.1:8002/capabilities' -TimeoutSec 20
  Confirm 'D8 /capabilities' ($r.Count -gt 0) "($($r.Count) capabilities)"
} catch { Confirm 'D8 /capabilities' $false $_.Exception.Message }
try {
  $r = Invoke-RestMethod 'http://127.0.0.1:8003/regulations' -TimeoutSec 20
  Confirm 'D9 /regulations' ($r.Count -gt 0) "($($r.Count) regulations)"
} catch { Confirm 'D9 /regulations' $false $_.Exception.Message }


Write-Output ''
Write-Output '=== 3. Domain 6 workflow stops at the HITL gate ==='
$query = 'EU Defense AI Tender: Autonomous reconnaissance agent with cross-border data transfer'
$sessionId = $null
try {
  $body = @{ query = $query } | ConvertTo-Json
  $w = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/workflows/start' -Method Post `
        -ContentType 'application/json' -Body $body -TimeoutSec 120
  $sessionId = $w.session_id
  Confirm 'workflow starts' ($null -ne $w.session_id) "session=$($w.session_id)"
  Confirm 'HITL gate engaged (not auto-approved)' ($w.pending_human_approval -eq $true) `
          "requires_human_approval=$($w.decision_report.requires_human_approval)"
  Confirm 'decision report produced' ($null -ne $w.decision_report.decision) `
          "decision=`"$($w.decision_report.decision)`" conf=$($w.decision_report.confidence) basis=$($w.decision_report.confidence_basis)"
} catch { Confirm 'workflow starts' $false $_.Exception.Message }

Write-Output ''
Write-Output '=== 4. Audit trail labels absent seams UNAVAILABLE (never fabricated) ==='
try {
  # /audit/tools returns { records: [...] }, not a bare array.
  $audit = (Invoke-RestMethod 'http://127.0.0.1:8000/audit/tools' -TimeoutSec 20).records
  foreach ($rec in $audit) {
    Write-Host ("        {0,-24} provider={1,-26} data_status={2}" -f $rec.tool_name, $rec.provider, $rec.data_status)
  }
  $absent = @('query_knowledge_graph', 'search_documents', 'query_company_context')
  $wrong = @($audit | Where-Object { $absent -contains $_.tool_name -and $_.data_status -ne 'UNAVAILABLE' })
  Confirm 'absent Person A seams report UNAVAILABLE' ($wrong.Count -eq 0) `
          "($($absent.Count - $wrong.Count)/$($absent.Count) correct)"
  $live = @($audit | Where-Object { $_.data_status -eq 'LIVE' })
  Confirm 'own-domain calls report LIVE' ($live.Count -ge 3) "($($live.Count) live calls)"
} catch { Confirm 'audit trail readable' $false $_.Exception.Message }

Write-Output ''
Write-Output '=== 5. HITL approve completes the workflow ==='
if ($sessionId) {
  try {
    $ap = @{ session_id = $sessionId; approved = $true; feedback = 'smoke-test sign-off'; reviewer_id = 'smoke-test' } | ConvertTo-Json
    $f = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/workflows/approve' -Method Post `
          -ContentType 'application/json' -Body $ap -TimeoutSec 120
    Confirm 'approve resumes to completion' ($f.is_completed -eq $true) "pending=$($f.pending_human_approval)"
  } catch { Confirm 'approve resumes to completion' $false $_.Exception.Message }
} else {
  Confirm 'approve resumes to completion' $false 'skipped: no session from step 3'
}

Write-Output ''
Write-Output '=== 6. Idempotent start replays instead of double-billing ==='
try {
  $b = @{ query = 'Idempotency probe query for market analysis'; idempotency_key = 'smoke-idem-001' } | ConvertTo-Json
  $first  = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/workflows/start' -Method Post -ContentType 'application/json' -Body $b -TimeoutSec 120
  $second = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/workflows/start' -Method Post -ContentType 'application/json' -Body $b -TimeoutSec 120
  Confirm 'replay returns original session' ($first.session_id -eq $second.session_id) "session=$($first.session_id)"
  Confirm 'second call flagged replayed=true' ($second.replayed -eq $true) "first.replayed=$($first.replayed) second.replayed=$($second.replayed)"
} catch { Confirm 'idempotent start' $false $_.Exception.Message }

Write-Output ''
Write-Output '=== 7. Known open item: auth is OFF by default (reported, not a FAIL) ==='
try {
  $null = Invoke-RestMethod 'http://127.0.0.1:8001/intelligence/competitors?topic=sovereign-ai' -TimeoutSec 20
  Write-Host "  NOTE  a protected endpoint answered with no API key. api.auth.enabled defaults to false." -ForegroundColor Yellow
  Write-Host "        Turn it on before anything is network-reachable. See docs/system-review.md." -ForegroundColor Yellow
} catch {
  Write-Host "  NOTE  protected endpoint rejected an unauthenticated call (auth active)." -ForegroundColor Green
}
Write-Host "  NOTE  1 pytest is skipped unless VEXER_PG_TEST_DSN is set; the persistence layer is" -ForegroundColor Yellow
Write-Host "        therefore NOT validated by a green suite. Run docker compose up -d postgres." -ForegroundColor Yellow

Write-Output ''
Write-Output ('=== RESULT: ' + $pass + ' passed, ' + $fail + ' failed ===')

Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if ($fail -gt 0) { exit 1 } else { exit 0 }

