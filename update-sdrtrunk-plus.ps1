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
# Gradle's output goes to update-log.txt next to this folder. While it builds, a line shows the time so far,
# a bar based on how long the last build took (kept in update-build-time.txt) and what Gradle is doing.

param(
    [string]$TranscriberDir = 'E:\SDR-Stuff\sdrtrunk-transcriber',
    [string]$Branch = 'master'
)

# Stopping the background (boot) task and closing the programs it started needs admin rights. When this
# was started without them, open a new admin window (Windows asks Yes/No) that runs this same script, then
# leave with exit code 10 so update-sdrtrunk-plus.bat closes this window without waiting for a key.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'The updater needs admin rights. Click Yes in the Windows box that pops up.' -ForegroundColor Yellow
    function Quote([string]$text) { "'" + $text.Replace("'", "''") + "'" }
    # The admin window runs the script, then waits for Enter so its messages can be read.
    $command = "Set-Location -LiteralPath $(Quote $PSScriptRoot); " +
        "& $(Quote $PSCommandPath) -TranscriberDir $(Quote $TranscriberDir) -Branch $(Quote $Branch); " +
        "Write-Host ''; Read-Host 'Press Enter to close this window'"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -WorkingDirectory $PSScriptRoot -ErrorAction Stop `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
    } catch {
        Write-Host 'Windows did not give admin rights (No was clicked), so nothing was changed.' -ForegroundColor Red
        Write-Host 'Run update-sdrtrunk-plus.bat again and click Yes.' -ForegroundColor Red
        exit 1
    }
    exit 10
}

$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
$parent = Split-Path $repo -Parent
$imageDir = Join-Path $repo 'build\image'
$previousImage = Join-Path $parent 'sdrtrunk-previous-image'
$log = Join-Path $parent 'update-log.txt'
$buildTimeFile = Join-Path $parent 'update-build-time.txt'
# Background mode (boot-on.bat): while this file is there the watchdog restarts nothing, and the boot
# task doesn't count the programs being closed as a failure.
$bootTaskName = 'SDRTrunk Plus (boot)'
$updatingFlag = Join-Path $TranscriberDir 'updating.flag'
$bootTask = Get-ScheduledTask -TaskName $bootTaskName -ErrorAction SilentlyContinue

# Files and folders in the transcriber folder that belong to you, not to the update.
$keepFiles = @('config.toml', 'vocabulary.txt', 'codes.csv', 'corrections.csv', 'places.csv',
               'places-geo.json', 'paging_tones.csv', '*.bak-*')
$keepDirs = @('data', 'ab-samples', 'blank-samples', '.venv', '511pa-probe', '__pycache__', '.pytest_cache')

$script:stepNumber = 0
$stepCount = 4
function Step($text) {
    $script:stepNumber++
    Write-Host ''
    Write-Host "== Step $($script:stepNumber) of ${stepCount}: $text" -ForegroundColor Cyan
}
function Format-Time([double]$seconds) {
    $t = [TimeSpan]::FromSeconds([math]::Round($seconds))
    if ($t.TotalHours -ge 1) { return '{0}:{1:mm}:{1:ss}' -f [int][math]::Floor($t.TotalHours), $t }
    return '{0}:{1:ss}' -f [int][math]::Floor($t.TotalMinutes), $t
}
# Runs the Gradle build, showing a line that updates every second until it finishes. Returns its exit code.
function Invoke-Build {
    $last = $null
    if (Test-Path $buildTimeFile) {
        try { $last = [double](Get-Content $buildTimeFile -TotalCount 1) } catch { $last = $null }
    }
    if ($last) { Note "The last build took $(Format-Time $last)." } else { Note 'First timed build, so there is no estimate yet.' }
    $proc = Start-Process -FilePath 'cmd.exe' -NoNewWindow -PassThru `
        -ArgumentList "/c cd /d `"$repo`" && gradlew.bat runtimeZipCurrent > `"$log`" 2>&1"
    $null = $proc.Handle    # keeps the exit code readable after it ends
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $width = 79
    try { $width = [math]::Max(40, [Console]::WindowWidth - 1) } catch { }
    while (-not $proc.HasExited) {
        $elapsed = $watch.Elapsed.TotalSeconds
        $line = "   Building: $(Format-Time $elapsed) so far"
        if ($last) {
            $fraction = [math]::Min(0.99, $elapsed / $last)
            $filled = [int](20 * $fraction)
            $line += "  [" + ('#' * $filled) + ('-' * (20 - $filled)) + "] $([int](100 * $fraction))%"
            if ($elapsed -lt $last) { $line += "  about $(Format-Time ($last - $elapsed)) left" } else { $line += '  almost done' }
        }
        # What Gradle is on, e.g. "> Task :compileJava".
        $task = Get-Content $log -Tail 40 -ErrorAction SilentlyContinue | Where-Object { $_ -like '> Task *' } | Select-Object -Last 1
        if ($task) { $line += "  $($task.Substring(7))" }
        if ($line.Length -gt $width) { $line = $line.Substring(0, $width) }
        Write-Host -NoNewline ("`r" + $line.PadRight($width))
        Start-Sleep -Seconds 1
    }
    $proc.WaitForExit()
    $seconds = $watch.Elapsed.TotalSeconds
    Write-Host ("`r" + "   Build finished in $(Format-Time $seconds)".PadRight($width))
    if ($proc.ExitCode -eq 0) { Set-Content -Path $buildTimeFile -Value ([math]::Round($seconds)) }
    return $proc.ExitCode
}
function Note($text) { Write-Host "   $text" }
function Start-BootTask {
    Remove-Item $updatingFlag -ErrorAction SilentlyContinue
    try {
        Start-ScheduledTask -TaskName $bootTaskName -ErrorAction Stop
        return $true
    } catch { return $false }
}
function Stop-Update($text) {
    Write-Host ''
    Write-Host $text -ForegroundColor Red
    if ($bootTask) {
        if (Start-BootTask) { Write-Host 'Everything is starting again in the background.' -ForegroundColor Yellow }
        else { Write-Host 'Restart the PC to start everything again.' -ForegroundColor Yellow }
    }
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
if ($bootTask) {
    Set-Content -Path $updatingFlag -Value 'update-sdrtrunk-plus.bat is updating'
    Note 'Background mode: the watchdog now leaves everything alone until the update is done.'
    Note 'The programs have no windows, so type C below to close them.'
    try { Stop-ScheduledTask -TaskName $bootTaskName -ErrorAction Stop } catch { }
    Start-Sleep -Seconds 2
}
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
    if ($answer -notmatch '^[yY]') {
        Write-Host ''
        Write-Host 'Nothing to do.' -ForegroundColor Green
        if ($bootTask) {
            if (Start-BootTask) { Write-Host 'Everything is starting again in the background.' -ForegroundColor Green }
            else { Write-Host 'Restart the PC to start everything again.' -ForegroundColor Yellow }
        }
        exit 0
    }
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
$buildFailed = (Invoke-Build) -ne 0
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
    Write-Host '   start-everything.bat and boot-on.bat find the new folder by themselves. Any other shortcut to SDRTrunk needs the new path.' -ForegroundColor Yellow
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

# The tray icon keeps running its old copy of tray.ps1: swap it for the new one. explorer.exe starts it
# as the signed-in user, not as admin like this window.
$trayScript = Join-Path $TranscriberDir 'start-tray.bat'
try {
    $oldTray = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -like '*tray.ps1*' })
    if ($oldTray.Count -gt 0 -and (Test-Path $trayScript)) {
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
            Where-Object { $_.CommandLine -like '*pc_speakers*' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        $oldTray | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        Start-Process explorer.exe "`"$trayScript`""
        Note 'Restarted the tray icon so its menu is up to date'
    }
} catch {
    Write-Host '   Could not restart the tray icon. Right-click it, Close this icon, then run start-tray.bat.' -ForegroundColor Yellow
}

Write-Host ''
Write-Host "Done. Updated $($old.Substring(0, 7)) to $($new.Substring(0, 7))." -ForegroundColor Green
if ($bootTask) {
    # boot-on.bat is in use: start everything again the same way, with no windows.
    if (Start-BootTask) {
        Write-Host 'Everything is starting again in the background (no windows), as at boot.' -ForegroundColor Green
        Write-Host 'The tray icon by the clock turns green when it is all running (a minute or two).' -ForegroundColor Green
    } else {
        Write-Host 'Restart the PC to start everything again in the background.' -ForegroundColor Green
    }
} else {
    Write-Host 'Start SDRTrunk and the transcriber again with your usual batch file.' -ForegroundColor Green
}
