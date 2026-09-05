# Dev server control for Windows: the PowerShell twin of dev.sh, and it keeps
# dev.sh's one rule - stop what this script started, by pid, never everything
# on the box whose command line happens to say "quorum".
#
#   .\dev.ps1 start | stop | restart | log [n]
param(
    [ValidateSet("start", "stop", "restart", "log")]
    [string]$Command = "restart",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$PidFile = ".dev.pid"
$Log     = "data\dev.log"
$Py      = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Get-DevProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $id = (Get-Content $PidFile -Raw).Trim()
    return Get-Process -Id $id -ErrorAction SilentlyContinue
}

switch ($Command) {
    "start" {
        $running = Get-DevProcess
        if ($running) { Write-Host "already up ($($running.Id))"; exit 0 }
        if (-not (Test-Path $Py)) { Write-Error "no .venv at $Py - see README.md (Setup -> On Windows)"; exit 1 }
        New-Item -ItemType Directory -Force -Path data | Out-Null

        $serveArgs = @("-m", "quorum", "serve") + $Rest
        $p = Start-Process -FilePath $Py -ArgumentList $serveArgs -NoNewWindow -PassThru `
                           -RedirectStandardOutput $Log -RedirectStandardError "$Log.err"
        $p.Id | Set-Content $PidFile

        # Wait for /health rather than for a fixed sleep: the interesting
        # failure is a server that exits during startup, and a poll notices
        # that in a second instead of reporting success at a dead port.
        $up = $false
        foreach ($i in 1..40) {
            Start-Sleep -Milliseconds 250
            try {
                Invoke-RestMethod -Uri "http://127.0.0.1:8600/health" -TimeoutSec 2 | Out-Null
                $up = $true; break
            } catch { }
        }
        if (-not $up) {
            Write-Host "did not come up:"
            if (Test-Path "$Log.err") { Get-Content "$Log.err" -Tail 20 }
            if (Test-Path $Log) { Get-Content $Log -Tail 20 }
            exit 1
        }
        Write-Host " <- pid $($p.Id)"
    }
    "stop" {
        $running = Get-DevProcess
        if ($running) { Stop-Process -Id $running.Id -Force -ErrorAction SilentlyContinue }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 600
    }
    "restart" {
        & $PSCommandPath stop
        & $PSCommandPath start @Rest
    }
    "log" {
        $n = if ($Rest.Count -gt 0) { [int]$Rest[0] } else { 40 }
        if (Test-Path $Log) { Get-Content $Log -Tail $n }
        if (Test-Path "$Log.err") { Get-Content "$Log.err" -Tail $n }
    }
}
