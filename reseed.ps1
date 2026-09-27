# One-command CareLink session recovery, for when the Railway uploader logs "reauth failed: HTTP 401"
# plus "realtime: HTTP 401". You only log in; capture, push, verification and cleanup are automatic.
# Needs a line RAILWAY_TOKEN=<Railway project token, production> in secrets.local (gitignored).
# Usage, from this folder:   .\reseed.ps1

$Service       = 'carelink-uploader'
$Python        = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$Chrome        = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$ChromeProfile = Join-Path $PSScriptRoot '.chrome-login'
$LoginUrl      = 'https://carelink.minimed.eu/patient/sso/login?country=il&lang=en'
# The native binary, not the npm .ps1 shim: stdin piped through shim -> node -> exe is not reliable.
$Railway       = Join-Path $env:APPDATA 'npm\node_modules\@railway\cli\bin\railway.exe'
if (-not (Test-Path $Railway)) { $Railway = 'railway' }

Set-Location $PSScriptRoot

function Get-JwtIssuedAt([string]$Jwt) {
  try {
    $p = $Jwt.Split('.')[1].Replace('-', '+').Replace('_', '/')
    switch ($p.Length % 4) { 2 { $p += '==' } 3 { $p += '=' } }
    $claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p)) | ConvertFrom-Json
    if ($null -eq $claims.iat) { return $null }
    return [long]$claims.iat
  } catch { return $null }
}

function Get-UploaderLog {
  return ((& $Railway logs --service $Service --lines 150 2>$null | Out-String) -split "`r?`n")
}

function Get-LinesAfter($Log, [string]$Pattern) {
  for ($i = $Log.Count - 1; $i -ge 0; $i--) {
    if ($Log[$i] -match $Pattern) {
      if ($i -ge $Log.Count - 1) { return @() }
      return $Log[($i + 1)..($Log.Count - 1)]
    }
  }
  return $null
}

$tokenLine = $null
if (Test-Path 'secrets.local') {
  $tokenLine = Get-Content 'secrets.local' | Where-Object { $_ -match '^\s*RAILWAY_TOKEN\s*=' } | Select-Object -First 1
}
if (-not $tokenLine) { Write-Host 'ERROR: add a line RAILWAY_TOKEN=<project token> to secrets.local'; exit 1 }
$env:RAILWAY_TOKEN = ($tokenLine -split '=', 2)[1].Trim()

try {
  $startedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

  Start-Process $Chrome -ArgumentList '--remote-debugging-port=9222', "--user-data-dir=`"$ChromeProfile`"", $LoginUrl
  Write-Host 'Chrome opened - log in to CareLink. Waiting for a fresh session (up to 6 min)...'

  # Only accept a token issued after this run started: the Chrome profile can still hold one from a
  # previous login, and pushing that stale session would just reproduce the 401s.
  $fresh = $false
  $deadline = (Get-Date).AddMinutes(6)
  while ((Get-Date) -lt $deadline) {
    & $Python web_session.py capture *> $null
    if (Test-Path 'web_state.json') {
      $tok = (Get-Content 'web_state.json' -Raw | ConvertFrom-Json) |
        Where-Object { $_.name -eq 'auth_tmp_token' -and $_.value } | Select-Object -First 1
      if ($tok) {
        $iat = Get-JwtIssuedAt $tok.value
        if ($null -eq $iat -or $iat -ge ($startedAt - 60)) { $fresh = $true; break }
      }
    }
    Start-Sleep -Seconds 5
  }
  if (-not $fresh) { Write-Host 'ERROR: no fresh CareLink login detected - run again and log in within 6 minutes.'; exit 1 }
  Write-Host 'Login captured.'

  & $Python web_session.py export *> $null
  if (-not (Test-Path 'cookie_jar.txt')) { Write-Host 'ERROR: cookie export failed.'; exit 1 }

  # Via stdin so the session never appears on a command line or in any output.
  $OutputEncoding = New-Object System.Text.UTF8Encoding $false
  Get-Content 'cookie_jar.txt' -Raw | & $Railway variable set COOKIE_JAR --stdin --service $Service --skip-deploys *> $null
  if ($LASTEXITCODE -ne 0) { Write-Host 'ERROR: could not set COOKIE_JAR on Railway.'; exit 1 }

  # Round-trip check before triggering the reseed: if stdin didn't carry the value intact, RESEED=1 would
  # boot the uploader on a broken jar. Compared in memory only - nothing is printed.
  $match = $false
  try {
    $remote = (& $Railway variable list --service $Service --json 2>$null | Out-String) | ConvertFrom-Json
    $match = [bool]$remote.COOKIE_JAR -and ($remote.COOKIE_JAR.Trim() -ceq (Get-Content 'cookie_jar.txt' -Raw).Trim())
  } catch { $match = $false }
  $remote = $null
  if (-not $match) { Write-Host 'ERROR: COOKIE_JAR on Railway does not match the captured session - not reseeding.'; exit 1 }

  & $Railway variable set RESEED=1 --service $Service *> $null
  if ($LASTEXITCODE -ne 0) { Write-Host 'ERROR: could not set RESEED on Railway.'; exit 1 }
  Remove-Item 'cookie_jar.txt' -ErrorAction SilentlyContinue
  Write-Host 'Pushed to Railway - waiting for the uploader to redeploy and confirm (up to 5 min)...'

  $result = 'timeout'
  $deadline = (Get-Date).AddMinutes(5)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 15
    $after = Get-LinesAfter (Get-UploaderLog) 'from COOKIE_JAR env \(seed\)'
    if ($null -ne $after) {
      if ($after -match 'reauth OK') { $result = 'ok'; break }
      if ($after -match 'reauth failed|HTTP 401') { $result = 'failed'; break }
    }
  }
  if ($result -eq 'failed') { Write-Host 'FAILED: the uploader rejected the new session (401). Log in again and re-run.'; exit 1 }
  if ($result -ne 'ok') { Write-Host 'TIMEOUT: no confirmation in the Railway logs yet - check them manually before re-running.'; exit 1 }
  Write-Host 'Uploader accepted the new session.'

  # The uploader persisted the session to Firestore on that boot. Leaving RESEED set would make every
  # future restart revert to this static snapshot (which expires); the jar itself is no longer needed.
  & $Railway variable delete RESEED --service $Service *> $null
  & $Railway variable delete COOKIE_JAR --service $Service *> $null
  # Deleting variables does not redeploy, and the running container keeps RESEED=1 in its env - a crash
  # restart would reload the (by then expired) snapshot. A fresh deployment picks up the clean variables.
  & $Railway redeploy --service $Service --yes *> $null
  Write-Host 'Removed RESEED and COOKIE_JAR, redeploying - confirming the uploader now runs from Firestore (up to 5 min)...'

  $deadline = (Get-Date).AddMinutes(5)
  $settled = $false
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 15
    $after = Get-LinesAfter (Get-UploaderLog) 'loaded \d+ cookies from Firestore'
    if ($null -ne $after -and $after -match 'reauth OK') { $settled = $true; break }
  }
  if ($settled) { Write-Host 'SUCCESS: session restored and running from Firestore.' }
  else { Write-Host 'Session restored, but the final restart has not confirmed yet - glance at the Railway logs in a few minutes.' }
}
finally {
  Remove-Item Env:RAILWAY_TOKEN -ErrorAction SilentlyContinue
}
