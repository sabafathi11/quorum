# Get one session's videos out of Google Drive and into its folder here.
# The PowerShell twin of fetch_session.sh, for the Windows side of the handoff.
#
#   .\fetch_session.ps1 20260811_033622
#   .\fetch_session.ps1 20260811_033622 C:\Users\you\Downloads\session_20260811_033622.zip
#
# The second form needs no rclone: download the zip from the shared
# beefline_records Drive folder and hand this script the path. Either way the
# videos land in the same place, which is what lets the documented layout be
# the same on both sides.
param(
    [Parameter(Mandatory = $true)][string]$Stamp,
    [string]$Zip = ""
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Stamp = $Stamp -replace '^session_', ''
$Dest = "session_$Stamp"
# `??` is PowerShell 7+; Windows still ships 5.1 by default, so spell it out.
$RemoteName = if ($env:RCLONE_REMOTE) { $env:RCLONE_REMOTE } else { "gdrive" }
$Remote = "${RemoteName}:beefline_records/session_$Stamp.zip"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

$temp = $false
if (-not $Zip) {
    if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
        Write-Host "rclone is not installed and no local zip was given."
        Write-Host "Download session_$Stamp.zip from the shared beefline_records Drive folder, then:"
        Write-Host "  .\fetch_session.ps1 $Stamp C:\path\to\session_$Stamp.zip"
        exit 1
    }
    $Zip = Join-Path $Dest ".session.zip"
    Write-Host "fetching $Remote"
    rclone copyto $Remote $Zip --progress
    if ($LASTEXITCODE -ne 0) { Write-Error "rclone failed"; exit 1 }
    $temp = $true
}

Write-Host "unpacking into $Dest\"
# Expand-Archive has no flatten flag, so unpack aside and move the mp4s up:
# a zip rebuilt with a leading directory would otherwise nest them one level
# too deep and every path in session.json would be wrong.
$stage = Join-Path $env:TEMP "beefline_$Stamp"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
Expand-Archive -Path $Zip -DestinationPath $stage -Force
Get-ChildItem $stage -Recurse -Filter *.mp4 | ForEach-Object {
    Move-Item $_.FullName (Join-Path $Dest $_.Name) -Force
}
Remove-Item $stage -Recurse -Force
if ($temp) { Remove-Item $Zip -Force }

Get-ChildItem "$Dest\*.mp4" | Select-Object Name, Length
Write-Host ""
Write-Host "Next: these are HEVC, which no browser decodes. Build the capture and then"
Write-Host "run Prepare video (Capture workspace -> Streams), or the cells stay black."
Write-Host "The SAM annotations are already in $Dest\sam\ - see README.md."
