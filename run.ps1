# Start the Quorum server on Windows. The PowerShell twin of run.sh.
#
# Two things differ from the shell script and neither is cosmetic: a Windows
# venv puts the interpreter at `.venv\Scripts\python.exe` rather than
# `.venv/bin/python`, and there is no system numpy to inherit, so the venv has
# to be a real install of this package. See the Windows block in README.md.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "no .venv at $py - see README.md (Setup -> On Windows)"
    exit 1
}

# ffmpeg is a soft dependency everywhere else in this codebase, but on a laptop
# that is building captures from video files it is the difference between the
# whole path working and every Build refusing. Say so once, here, rather than
# leaving it to a toast four clicks later.
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Warning "ffprobe is not on PATH - capture building, video preparation and still frames will refuse. Install with: winget install Gyan.FFmpeg"
}

& $py -m quorum serve @args
exit $LASTEXITCODE
