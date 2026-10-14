param(
  [string]$ApiBase = "http://127.0.0.1:8002/api",
  [string[]]$Symbols = @("EURUSD", "XAUUSD", "US500"),
  [int]$MaxAgeSeconds = 30
)

$ErrorActionPreference = "Stop"

function Fail($Message) {
  Write-Error $Message
  exit 1
}

$status = Invoke-RestMethod -Uri "$ApiBase/status" -TimeoutSec 20
if (-not $status.connected) {
  Fail "Backend is not connected to a broker."
}
$Symbols = @(
  foreach ($item in $Symbols) {
    foreach ($part in ('' + $item).Split(',')) {
      $clean = $part.Trim()
      if ($clean) { $clean }
    }
  }
)
if ($Symbols.Count -eq 0) {
  Fail "No symbols supplied for live price check."
}

if ($status.platform -ne "mt5") {
  Fail "Expected MT5 platform, got '$($status.platform)'."
}

$joined = [System.Uri]::EscapeDataString(($Symbols -join ","))
$response = Invoke-RestMethod -Uri "$ApiBase/market/ticks?symbols=$joined" -TimeoutSec 20
if ($response.source -ne "mt5_broker_tick") {
  Fail "Expected mt5_broker_tick source, got '$($response.source)'."
}

$nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$missing = @()
$stale = @()
$invalid = @()

foreach ($symbol in $Symbols) {
  $tick = $response.ticks.PSObject.Properties[$symbol].Value
  if ($null -eq $tick) {
    $missing += $symbol
    continue
  }

  $bid = [double]($tick.bid)
  $ask = [double]($tick.ask)
  if ([double]::IsNaN($bid) -or [double]::IsInfinity($bid) -or [double]::IsNaN($ask) -or [double]::IsInfinity($ask) -or $bid -le 0 -or $ask -le 0 -or $ask -lt $bid) {
    $invalid += "$symbol bid=$bid ask=$ask"
    continue
  }

  $tickMs = 0L
  if ($tick.received_at_msc -and [int64]$tick.received_at_msc -gt 1000000000000) {
    $tickMs = [int64]$tick.received_at_msc
  } elseif ($tick.time_msc -and [int64]$tick.time_msc -gt 1000000000000) {
    $tickMs = [int64]$tick.time_msc
  } elseif ($tick.time) {
    $tickMs = [int64]$tick.time * 1000
  }
  $ageSeconds = ($nowMs - $tickMs) / 1000.0
  if ($tickMs -le 0 -or $ageSeconds -gt $MaxAgeSeconds) {
    $stale += "$symbol age=$([Math]::Round($ageSeconds, 1))s"
  }
}

if ($missing.Count -gt 0) {
  Fail "Missing live ticks: $($missing -join ', ')"
}
if ($invalid.Count -gt 0) {
  Fail "Invalid live ticks: $($invalid -join '; ')"
}
if ($stale.Count -gt 0) {
  Fail "Stale live ticks: $($stale -join '; ')"
}

Write-Output "Live price check passed: source=$($response.source), symbols=$($Symbols -join ', '), max_age=${MaxAgeSeconds}s"
