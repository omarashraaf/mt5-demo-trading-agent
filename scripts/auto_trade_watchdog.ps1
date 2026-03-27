$ErrorActionPreference = "Stop"

param(
  [string]$ApiBase = "http://127.0.0.1:8002/api",
  [int]$IntervalMinutes = 10,
  [string]$LogPath = "C:\Users\oashraf00\tradingagent\backend\auto_trade_watchdog.log"
)

function Get-ReasonBucket {
  param([string]$Detail)
  if ([string]::IsNullOrWhiteSpace($Detail)) { return "unknown" }
  $d = $Detail.ToLowerInvariant()
  if ($d.Contains("spread")) { return "spread" }
  if ($d.Contains("caps") -or $d.Contains("trade(s) in the recent trading window")) { return "symbol_trade_cap" }
  if ($d.Contains("clean entry zone") -or $d.Contains("chop/mean") -or $d.Contains("too noisy")) { return "entry_quality" }
  if ($d.Contains("inactive in the current mode")) { return "inactive_mode" }
  if ($d.Contains("margin")) { return "margin" }
  if ($d.Contains("not currently tradeable")) { return "not_tradeable" }
  return "other"
}

function Write-WatchdogLog {
  param([string]$ApiBase, [string]$LogPath)

  $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  try {
    $status = Invoke-RestMethod -Method Get -Uri "$ApiBase/auto-trade/status" -TimeoutSec 15
    $activity = Invoke-RestMethod -Method Get -Uri "$ApiBase/auto-trade/activity?limit=120" -TimeoutSec 20

    $live = @($activity.live_activity)
    $recent = @($status.recent_trades)
    $holds = @($live | Where-Object { $_.action -eq "HOLD" })
    $fails = @($recent | Where-Object { $_.success -eq $false })
    $ok = @($recent | Where-Object { $_.success -eq $true })
    $spreadFails = @($fails | Where-Object { "$($_.detail)".ToLowerInvariant().Contains("spread") })

    $bucketCounts = @{}
    foreach ($h in $holds) {
      $bucket = Get-ReasonBucket -Detail "$($h.detail)"
      if (-not $bucketCounts.ContainsKey($bucket)) { $bucketCounts[$bucket] = 0 }
      $bucketCounts[$bucket] += 1
    }

    $summary = [ordered]@{
      ts = $timestamp
      running = [bool]$status.running
      enabled = [bool]$status.enabled
      connected = $true
      scan_interval = [int]$status.scan_interval
      min_confidence = [double]$status.min_confidence
      recent_total = $recent.Count
      recent_success = $ok.Count
      recent_failed = $fails.Count
      spread_failed = $spreadFails.Count
      hold_count = $holds.Count
      hold_buckets = $bucketCounts
    }

    $line = ($summary | ConvertTo-Json -Compress)
    Add-Content -Path $LogPath -Value $line
    Write-Output $line
  } catch {
    $err = [ordered]@{
      ts = $timestamp
      connected = $false
      error = $_.Exception.Message
    } | ConvertTo-Json -Compress
    Add-Content -Path $LogPath -Value $err
    Write-Output $err
  }
}

while ($true) {
  Write-WatchdogLog -ApiBase $ApiBase -LogPath $LogPath
  Start-Sleep -Seconds ($IntervalMinutes * 60)
}
