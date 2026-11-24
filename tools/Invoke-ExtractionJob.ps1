#Requires -Version 7.0
<#
.SYNOPSIS
    Runs the Python extractor for several entity sets, writes a run summary CSV and exits with the worst status.

.DESCRIPTION
    Operational wrapper for scheduled runs (for example Windows Task Scheduler or a cron job with pwsh). It holds no
    extraction or integrity logic: it calls `python -m odata_extractor extract` once per entity set and reads the
    report.json each run produces. Exit code: 0 all COMPLETE, 3 worst INDETERMINATE, 4 worst INCOMPLETE,
    5 worst FAILED (also used when an extraction produced no report).

.EXAMPLE
    ./tools/Invoke-ExtractionJob.ps1 -JobFile ./examples/job.json

    job.json:
    { "baseUrl": "http://127.0.0.1:8765/odata/", "outputRoot": "output/runs",
      "entities": ["WorkOrders", "Technicians"], "extraArgs": ["--page-size", "50"] }
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $JobFile,
    [string] $Python = 'python',
    [string] $RunId = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$job = Get-Content -LiteralPath $JobFile -Raw | ConvertFrom-Json
$runDir = Join-Path $job.outputRoot $RunId
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$severity = @{ COMPLETE = 0; INDETERMINATE = 3; INCOMPLETE = 4; FAILED = 5 }
$extraArgs = @($job.PSObject.Properties['extraArgs'] ? $job.extraArgs : @())

$rows = foreach ($entity in $job.entities) {
    & $Python -m odata_extractor extract --base-url $job.baseUrl --entity $entity --output $runDir @extraArgs | Out-Host
    $exitCode = $LASTEXITCODE
    $reportPath = Join-Path $runDir $entity 'report.json'
    $row = [ordered]@{ Entity = $entity; Status = 'FAILED'; ExitCode = $exitCode; UniqueRecords = $null; Requests = $null; Retries = $null }
    if (Test-Path -LiteralPath $reportPath) {
        $report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
        $row.Status = $report.integrity.status
        $row.UniqueRecords = $report.integrity.counts.unique_records
        $row.Requests = $report.metrics.requests_total
        $row.Retries = $report.metrics.retries
    }
    [pscustomobject]$row
}

$summary = Join-Path $runDir 'run-summary.csv'
$rows | Export-Csv -LiteralPath $summary -NoTypeInformation -Encoding utf8
$rows | Format-Table -AutoSize | Out-String | Write-Host
$worst = ($rows | ForEach-Object { $severity.ContainsKey($_.Status) ? $severity[$_.Status] : 5 } | Measure-Object -Maximum).Maximum
Write-Host "Run $RunId finished; worst status exit code $worst; summary $summary"
exit [int]$worst
