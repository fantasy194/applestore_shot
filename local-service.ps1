param([switch]$Stop, [switch]$NoOpen)
$ErrorActionPreference = 'Stop'
$runtimeDirectory = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.local-runtime\duo-killer-web.exe')) { '.local-runtime' } else { 'local-web-release' }
$executable = Join-Path $PSScriptRoot ($runtimeDirectory + '\duo-killer-web.exe')
$data = Join-Path $PSScriptRoot '.local-data'
$pidFile = Join-Path $data 'server.pid'
$logFile = Join-Path $data 'server.log'
$running = $null
if (Test-Path -LiteralPath $pidFile) {
    $running = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile)) -ErrorAction SilentlyContinue
    if ($running -and $running.Path -ne $executable) { $running = $null }
}
if ($Stop) {
    if ($running) {
        Stop-Process -Id $running.Id -Force
        Remove-Item -LiteralPath $pidFile -Force
        Write-Host 'DUO Killer stopped.'
    } else { Write-Host 'DUO Killer is not running.' }
    exit 0
}
if (-not $running) {
    if (-not (Test-Path -LiteralPath $executable)) { throw 'Build missing: local-web-release\duo-killer-web.exe' }
    New-Item -ItemType Directory -Path $data -Force | Out-Null
    $running = Start-Process -FilePath $executable -ArgumentList @('--no-open', '--port', '17653', '--data-dir', ('"' + $data + '"')) -WorkingDirectory (Split-Path $executable) -WindowStyle Hidden -RedirectStandardOutput $logFile -RedirectStandardError (Join-Path $data 'server-error.log') -PassThru
    Set-Content -LiteralPath $pidFile -Value $running.Id
}
for ($attempt = 0; $attempt -lt 50; $attempt++) {
    $running.Refresh()
    if ($running.HasExited) { throw ('Server stopped. Check ' + (Join-Path $data 'server-error.log')) }
    $content = Get-Content -LiteralPath $logFile -Raw -ErrorAction SilentlyContinue
    if ($content -match 'http://127\.0\.0\.1:\d+/#access=[a-f0-9]{64}') {
        $launchUrl = $Matches[0]
        Set-Content -LiteralPath (Join-Path $data 'open-local.url') -Encoding ascii -Value @('[InternetShortcut]', ('URL=' + $launchUrl))
        if (-not $NoOpen) {
            Start-Process -FilePath 'rundll32.exe' -ArgumentList ('url.dll,FileProtocolHandler ' + $launchUrl) -WindowStyle Hidden
        }
        Write-Host 'DUO Killer is running. Open this complete local link:'
        Write-Host $launchUrl
        exit 0
    }
    Start-Sleep -Milliseconds 200
}
throw 'Server startup timed out.'
