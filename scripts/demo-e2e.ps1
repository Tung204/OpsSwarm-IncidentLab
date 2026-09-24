param(
    [string]$Scenario = "booking-api-high-5xx",
    [int]$WaitSeconds = 300,
    [switch]$Approve,
    [string]$EnterpriseUrl = "http://127.0.0.1:18088",
    [string]$GitHubRepo = $env:GITHUB_REPO
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Get-RunState([int]$IssueNumber) {
    try { return Invoke-RestMethod -Uri "$EnterpriseUrl/runs/$IssueNumber" -TimeoutSec 10 }
    catch { return $null }
}

Write-Host "=== OpsSwarm two-repository E2E ==="
Write-Host "IncidentLab: http://localhost:8080"
Write-Host "Enterprise:  $EnterpriseUrl"
Write-Host "Scenario:    $Scenario"

docker compose up --build -d
Start-Sleep -Seconds 6

$lab = Invoke-RestMethod -Uri "http://localhost:8080/health" -TimeoutSec 15
if (-not $lab.ok -or $lab.opsswarm_embedded) { throw "IncidentLab boundary check failed." }

try {
    $enterprise = Invoke-RestMethod -Uri "$EnterpriseUrl/health" -TimeoutSec 10
} catch {
    throw "OpsSwarm-Enterprise is unavailable at $EnterpriseUrl. This failure is expected when only IncidentLab is running; the E2E intentionally does not fall back to embedded orchestration."
}
if (-not $enterprise.ok) { throw "OpsSwarm-Enterprise health check failed." }

$start = Invoke-RestMethod -Method Post -Uri "http://localhost:8080/api/demo/start" -ContentType "application/json" -Body (@{scenario_id=$Scenario} | ConvertTo-Json)
if (-not $start.monitoring_delivery.delivered) { throw "IncidentLab could not deliver monitoring to OpsSwarm-Enterprise: $($start.monitoring_delivery.error)" }
$issue = [int]$start.github_issue_number
if (-not $issue) { throw "Enterprise monitoring ingress did not return a GitHub Issue number." }
Write-Host "GitHub Issue: #$issue"
if ($start.github_issue_url) { Write-Host "URL: $($start.github_issue_url)" }

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$run = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    $run = Get-RunState $issue
    if ($run) {
        Write-Host ("[{0}] state={1} findings={2} root_cause={3} plan={4} decision={5} execution={6} verification={7}" -f (Get-Date -Format "HH:mm:ss"), $run.state, @($run.findings).Count, [bool]$run.root_cause, [bool]$run.recovery_plan, [bool]$run.decision, [bool]$run.execution, [bool]$run.verification)
        if ($run.state -in @("WAITING_APPROVAL","WAITING_DECISION","WAITING_INPUT","RESOLVED","FAILED","ABORTED")) { break }
    }
}
if (-not $run) { throw "No Enterprise orchestration run was observed for Issue #$issue." }

if ($run.state -eq "WAITING_APPROVAL") {
    $option = $run.decision.options | Select-Object -First 1
    Write-Host "Required human command: /opsswarm approve $($option.id)"
    if ($Approve) {
        if ([string]::IsNullOrWhiteSpace($GitHubRepo)) { throw "-GitHubRepo or GITHUB_REPO is required for -Approve." }
        gh issue comment $issue --repo $GitHubRepo --body "/opsswarm approve $($option.id)"
        if ($LASTEXITCODE -ne 0) { throw "Failed to post the explicit GitHub approval command." }
        $deadline = (Get-Date).AddSeconds($WaitSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 5
            $run = Get-RunState $issue
            if ($run) {
                Write-Host ("[{0}] state={1} execution={2} verification={3}" -f (Get-Date -Format "HH:mm:ss"), $run.state, [bool]$run.execution, [bool]$run.verification)
                if ($run.state -in @("RESOLVED","FAILED","ABORTED")) { break }
            }
        }
    }
}

Write-Host ""
Write-Host "=== FINAL ==="
Write-Host "Issue #$issue => $($run.state)"
Write-Host "IncidentLab supplied the target system only; Enterprise owned orchestration and GitHub authority."

if ($run.state -eq "RESOLVED") { exit 0 }
if ($run.state -in @("FAILED", "ABORTED")) {
    throw "Two-repository E2E ended in terminal failure state: $($run.state)"
}
if ($Approve) {
    throw "Two-repository E2E did not reach RESOLVED within the requested wait window; final state: $($run.state)"
}
