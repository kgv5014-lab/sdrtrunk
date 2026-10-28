# One-click update for SDRTrunk Plus, started by update-sdrtrunk-plus.bat.
#
# 1. Waits until SDRTrunk and the transcriber are closed (or closes them when asked).
# 2. Gets the latest code from GitHub (git fetch + fast-forward, never overwrites local edits).
# 3. Builds SDRTrunk (gradlew runtimeZipCurrent). The old program is kept in sdrtrunk-previous-image
#    next to this folder and put back if the build fails.
# 4. Copies the new transcriber files into the transcriber folder. Settings and data are never copied
#    over: config.toml, vocabulary.txt, codes.csv, corrections.csv, places files, paging_tones.csv and the
#    data, ab-samples, blank-samples and .venv folders. Python packages are updated only when the
#    requirements files changed.
#
# Gradle's output goes to update-log.txt next to this folder.

param(
    [string]$TranscriberDir = 'E:\SDR-Stuff\sdrtrunk-transcriber',
    [string]$Branch = 'master'
)

$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
$parent = Split-Path $repo -Parent
$imageDir = Join-Path $repo 'build\image'
$previousImage = Join-Path $parent 'sdrtrunk-previous-image'
$log = Join-Path $parent 'update-log.txt'

# Files and folders in the transcriber folder that belong to you, not to the update.
$keepFiles = @('config.toml', 'vocabulary.txt', 'codes.csv', 'corrections.csv', 'places.csv',
               'places-geo.json', 'paging_tones.csv', '*.bak-*')
$keepDirs = @('data', 'ab-samples', 'blank-samples', '.venv', '511pa-probe', '__pycache__', '.pytest_cache')

function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "   $text" }
function Stop-Update($text) {
    Write-Host ''
    Write-Host $text -ForegroundColor Red
    exit 1
}

function Get-RunningPrograms {
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and (
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($imageDir, [StringComparison]::OrdinalIgnoreCase)) -or
            ($_.CommandLine -and $_.CommandLine -like '*sdrtrunk_transcriber*'))
    }
}

Write-Host 'SDRTrunk Plus updater' -ForegroundColor Green
Note "SDRTrunk code:      $repo"
Note "Transcriber folder: $TranscriberDir"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Stop-Update 'Git is not installed or not on PATH.' }
if (-not (Test-Path (Join-Path $repo 'gradlew.bat'))) { Stop-Update "No gradlew.bat in $repo. Keep this file in the sdrtrunk folder." }
if (-not (Test-Path $TranscriberDir)) { Stop-Update "Transcriber folder $TranscriberDir not found. Run: update-sdrtrunk-plus.bat -TranscriberDir `"<folder>`"" }

Step 'Checking that SDRTrunk and the transcriber are closed'
while ($running = @(Get-RunningPrograms)) {
    Note 'Still running:'
    foreach ($p in $running) { Note "  $($p.Name) (process $($p.ProcessId))" }
    $answer = Read-Host '   Close them yourself and press Enter, type C to close them for you, or Q to quit'
    if ($answer -match '^[qQ]') { Stop-Update 'Update cancelled. Nothing was changed.' }
    if ($answer -match '^[cC]') {
        foreach ($p in $running) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 3
    }
}
Note 'All closed.'

Step 'Getting the latest code from GitHub'
Push-Location $repo
$changed = git status --porcelain --untracked-files=no
if ($changed) {
    Note 'These files were edited on this PC:'
    $changed | ForEach-Object { Note "  $_" }
    Stop-Update 'The update stopped so it does not overwrite them. Nothing was changed.'
}
$old = (git rev-parse HEAD).Trim()
git fetch origin $Branch
if ($LASTEXITCODE -ne 0) { Stop-Update 'Could not reach GitHub. Check the internet connection. Nothing was changed.' }
if ((git rev-parse --abbrev-ref HEAD).Trim() -ne $Branch) {
    git checkout $Branch
    if ($LASTEXITCODE -ne 0) { Stop-Update "Could not switch to the $Branch branch. Nothing was changed." }
}
git merge --ff-only "origin/$Branch"
if ($LASTEXITCODE -ne 0) { Stop-Update "This PC has its own commits on $Branch, so the update stopped. Nothing was changed." }
$new = (git rev-parse HEAD).Trim()

if ($old -eq $new) {
    Note 'Already up to date.'
    $answer = Read-Host '   Rebuild and reinstall anyway? Type Y for yes, Enter to stop'
    if ($answer -notmatch '^[yY]') { Write-Host ''; Write-Host 'Nothing to do.' -ForegroundColor Green; exit 0 }
} else {
    Note 'New changes:'
    git log --oneline --no-merges "$old..$new" | ForEach-Object { Note "  $_" }
}

Step 'Building SDRTrunk (a few minutes)'
$oldImageName = $null
if (Test-Path $imageDir) {
    $oldImageName = (Get-ChildItem $imageDir -Directory | Select-Object -First 1).Name
    if (Test-Path $previousImage) { Remove-Item $previousImage -Recurse -Force }
    try { Move-Item $imageDir $previousImage -ErrorAction Stop }
    catch { Stop-Update "Could not move the old SDRTrunk aside. Is it still open? ($($_.Exception.Message))" }
    Note "Old program kept in $previousImage"
}
cmd /c "cd /d `"$repo`" && gradlew.bat runtimeZipCurrent > `"$log`" 2>&1"
$buildFailed = $LASTEXITCODE -ne 0
$newImage = $null
if (-not $buildFailed) {
    $newImage = Get-ChildItem $imageDir -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $newImage -or -not (Test-Path (Join-Path $newImage.FullName 'bin'))) { $buildFailed = $true }
}
if ($buildFailed) {
    Note 'Last lines of the build log:'
    Get-Content $log -Tail 30 | ForEach-Object { Note "  $_" }
    if (Test-Path $previousImage) {
        if (Test-Path $imageDir) { Remove-Item $imageDir -Recurse -Force }
        Move-Item $previousImage $imageDir
        Note 'Your old SDRTrunk is back in place and still works.'
    }
    Stop-Update "The build failed, so the transcriber was not updated either. Send Claude the file $log"
}
Note "Built $($newImage.Name)"
if ($oldImageName -and $oldImageName -ne $newImage.Name) {
    Write-Host "   The program folder name changed from $oldImageName to $($newImage.Name)." -ForegroundColor Yellow
    Write-Host '   Update the path in the batch file you use to start SDRTrunk.' -ForegroundColor Yellow
}

Step 'Installing the new transcriber files'
$source = Join-Path $repo 'transcriber'
$excludeDirs = $keepDirs | ForEach-Object { Join-Path $source $_ }
robocopy $source $TranscriberDir /E /XF $keepFiles /XD $excludeDirs /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Stop-Update "Copying the transcriber files failed (robocopy code $LASTEXITCODE). SDRTrunk itself is updated." }
Note 'Copied. Your settings, codes, vocabulary, places and call history were left alone.'

git diff --quiet $old $new -- transcriber/requirements.txt transcriber/requirements-gpu.txt
if ($LASTEXITCODE -ne 0) {
    Note 'The Python package list changed, updating packages'
    Push-Location $TranscriberDir
    & .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt -r requirements-gpu.txt
    $pipFailed = $LASTEXITCODE -ne 0
    Pop-Location
    if ($pipFailed) { Write-Host '   Package update failed. Run install.bat in the transcriber folder.' -ForegroundColor Yellow }
}
Pop-Location

Write-Host ''
Write-Host "Done. Updated $($old.Substring(0, 7)) to $($new.Substring(0, 7))." -ForegroundColor Green
Write-Host 'Start SDRTrunk and the transcriber again with your usual batch file.' -ForegroundColor Green
