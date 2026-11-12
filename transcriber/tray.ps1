# The SDRTrunk Plus tray icon, started when you sign in (boot-on.bat adds it to Startup).
#
# The dot by the clock shows how the background programs are doing, from watchdog-status.json:
#   green = all running, orange = starting, edit mode or updating, red = one stopped, grey = no watchdog.
# Right-click it for the map, edit mode, the PC speakers, restart, stop all, start (each program, with
# or without windows), update and the log.
# While you are signed in the programs run with their windows: the first time this icon sees them running
# with no windows (started at boot, or by the updater), it starts them again with windows. Untick
# "Open the windows when I'm signed in" in the menu to leave them hidden.
# It also plays the live calls on the PC speakers (sdrtrunk_transcriber.pc_speakers, no window).

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$here = $PSScriptRoot
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\SDRTrunkPlusTray', [ref]$created)
if (-not $created) { exit }  # already running

$python = Join-Path $here '.venv\Scripts\pythonw.exe'
$statusFile = Join-Path $here 'watchdog-status.json'
$restartFlag = Join-Path $here 'restart.flag'
$editFlag = Join-Path $here 'edit-mode.flag'
$updatingFlag = Join-Path $here 'updating.flag'
$speakersOffFile = Join-Path $here 'speakers-off.flag'
$resultFile = Join-Path $here 'control-result.txt'
$windowsOffFile = Join-Path $here 'windows-off.flag'
$script:wantWindows = -not (Test-Path $windowsOffFile)
# Windows are opened once per run of this icon.
$script:windowsDone = $false
$script:job = $null
$script:speakers = $null
$script:wantSpeakers = -not (Test-Path $speakersOffFile)
$script:status = $null
$script:lastLevel = ''
$script:port = 3000

function New-Dot([System.Drawing.Color]$color) {
    $bmp = New-Object System.Drawing.Bitmap 16, 16
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $g.FillEllipse((New-Object System.Drawing.SolidBrush $color), 1, 1, 14, 14)
    $g.DrawEllipse((New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(160, 0, 0, 0))), 1, 1, 14, 14)
    $g.Dispose()
    [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$icons = @{
    ok   = New-Dot ([System.Drawing.Color]::FromArgb(40, 170, 70))
    wait = New-Dot ([System.Drawing.Color]::FromArgb(240, 160, 30))
    bad  = New-Dot ([System.Drawing.Color]::FromArgb(210, 40, 40))
    off  = New-Dot ([System.Drawing.Color]::FromArgb(140, 140, 140))
}

$words = @{
    ok = 'running'; starting = 'starting'; down = 'stopped'; frozen = 'not responding'; unknown = 'checking'
    editing = 'edit mode (window open)'; paused = 'paused for an hour (kept failing)'; updating = 'updating'
}
$names = @{ Ollama = 'Summaries (Ollama)'; Transcriber = 'Whisper transcriber'; SDRTrunk = 'SDRTrunk' }

function Read-Status {
    try {
        $s = Get-Content $statusFile -Raw -ErrorAction Stop | ConvertFrom-Json
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$s.time
        if ($age -gt 180) { return $null }  # the watchdog stopped writing: it isn't running
        if ($s.port) { $script:port = [int]$s.port }
        return $s
    } catch { return $null }
}

function Get-Level($s) {
    if (-not $s) { return 'off' }
    $states = @($s.programs | ForEach-Object { $_.state })
    if ($states | Where-Object { $_ -in @('down', 'frozen', 'paused') }) { return 'bad' }
    if ($states | Where-Object { $_ -ne 'ok' }) { return 'wait' }
    return 'ok'
}

function Get-Lines($s) {
    if (-not $s) { return @('The watchdog is not running (Start > Everything starts it)') }
    @($s.programs | ForEach-Object {
        $n = $names[$_.name]; if (-not $n) { $n = $_.name }
        $w = $words[$_.state]; if (-not $w) { $w = $_.state }
        "${n}: $w"
    })
}

function Stop-AllSpeakers {
    # Every copy of the player, not only the one this icon started: a copy left behind by an earlier
    # tray icon (the updater swaps the icon) would otherwise keep playing, twice over with a window open.
    try {
        Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -like '*sdrtrunk_transcriber.pc_speakers*' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { }
    $script:speakers = $null
}

function Update-Speakers {
    # Only with SDRTrunk in the background (boot-on.bat); with its window, SDRTrunk plays the calls itself.
    $busy = (Test-Path $updatingFlag) -or -not ($script:status -and $script:status.background)
    $running = $script:speakers -and -not $script:speakers.HasExited
    if ($script:wantSpeakers -and -not $busy -and -not $running -and (Test-Path $python)) {
        $script:speakers = Start-Process -FilePath $python -WorkingDirectory $here -PassThru `
            -ArgumentList '-m', 'sdrtrunk_transcriber.pc_speakers', '--config', 'config.toml'
    } elseif ((-not $script:wantSpeakers -or $busy) -and $running) {
        Stop-Process -Id $script:speakers.Id -Force -ErrorAction SilentlyContinue
        Stop-AllSpeakers
    }
}

function Update-Tray {
    $script:status = Read-Status
    $level = Get-Level $script:status
    $tray.Icon = $icons[$level]
    $tip = switch ($level) {
        'ok'   { 'SDRTrunk Plus: all running' }
        'off'  { 'SDRTrunk Plus: watchdog not running' }
        default { 'SDRTrunk Plus: ' + ((Get-Lines $script:status | Where-Object { $_ -notlike '*: running' }) -join ', ') }
    }
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 60) + '...' }
    $tray.Text = $tip
    if ($level -eq 'bad' -and $script:lastLevel -ne 'bad') {
        $tray.ShowBalloonTip(8000, 'SDRTrunk Plus', ((Get-Lines $script:status) -join "`n"), [System.Windows.Forms.ToolTipIcon]::Warning)
    }
    $script:lastLevel = $level
    Open-Windows
    Update-Speakers
}

function Open-Windows {
    # Signed in (this icon only runs then) with everything running hidden: start it again with windows.
    if ($script:windowsDone -or -not $script:status) { return }
    if ((Test-Path $updatingFlag) -or (Test-Path $editFlag)) { return }
    if ($script:job -and -not $script:job.HasExited) { return }
    $script:windowsDone = $true
    if ($script:wantWindows -and $script:status.background -and (Test-Path $python)) {
        Start-Control @('start', 'everything', '--window') 'You are signed in: opening everything with its windows (about a minute)...'
    }
}

function Find-Updater {
    # sdr-trunk.bat is in <repo>\build\image\<version>\bin; the updater is in <repo>.
    $bat = if ($script:status) { [string]$script:status.sdrtrunk_bat } else { '' }
    $i = $bat.ToLower().IndexOf('\build\image\')
    if ($i -gt 0) {
        $updater = Join-Path $bat.Substring(0, $i) 'update-sdrtrunk-plus.bat'
        if (Test-Path $updater) { return $updater }
    }
    return $null
}

function Start-Control([string[]]$arguments, [string]$saying) {
    # Stop all and Start run sdrtrunk_transcriber.control with no window; its answer is shown when it's done.
    if ($script:job -and -not $script:job.HasExited) {
        [System.Windows.Forms.MessageBox]::Show('Still busy with the last Stop or Start. Try again in a minute.', 'SDRTrunk Plus') | Out-Null
        return
    }
    if (-not (Test-Path $python)) {
        [System.Windows.Forms.MessageBox]::Show('Python is missing here. Run install.bat in the transcriber folder.', 'SDRTrunk Plus') | Out-Null
        return
    }
    Remove-Item $resultFile -ErrorAction SilentlyContinue
    $script:job = Start-Process -FilePath $python -WorkingDirectory $here -PassThru `
        -ArgumentList (@('-m', 'sdrtrunk_transcriber.control', '--config', 'config.toml') + $arguments)
    $tray.ShowBalloonTip(4000, 'SDRTrunk Plus', $saying, [System.Windows.Forms.ToolTipIcon]::Info)
    $jobTimer.Start()
}

function Show-ControlResult {
    if (-not $script:job -or -not $script:job.HasExited) { return }
    $jobTimer.Stop()
    $script:job = $null
    $lines = @(Get-Content $resultFile -ErrorAction SilentlyContinue)
    if ($lines.Count -lt 2) { $lines = @('failed', 'It stopped without saying what happened. See the log.') }
    $kind = if ($lines[0] -eq 'failed') { [System.Windows.Forms.ToolTipIcon]::Warning } else { [System.Windows.Forms.ToolTipIcon]::Info }
    $tray.ShowBalloonTip(8000, 'SDRTrunk Plus', $lines[1], $kind)
    Update-Tray
}

$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = $icons.off
$tray.Text = 'SDRTrunk Plus'
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$tray.ContextMenuStrip = $menu

function Add-Item($text, [scriptblock]$action, $parent = $null) {
    $item = New-Object System.Windows.Forms.ToolStripMenuItem $text
    if ($action) { $item.add_Click($action) }
    if ($parent) { [void]$parent.DropDownItems.Add($item) } else { [void]$menu.Items.Add($item) }
    $item
}

function Add-StartMenu($text, [bool]$window) {
    # One submenu: Everything (the watchdog, which keeps them all running), then each program on its own.
    $sub = Add-Item $text $null
    $how = if ($window) { 'with windows' } else { 'with no windows' }
    foreach ($p in @(
            @('Everything (kept running by the watchdog)', 'everything', 'everything'),
            @('SDRTrunk only', 'sdrtrunk', 'SDRTrunk'),
            @('Whisper transcriber only', 'transcriber', 'the Whisper transcriber'),
            @('Summaries (Ollama) only', 'ollama', 'Ollama'))) {
        $item = Add-Item $p[0] $null $sub
        $item.Tag = @{ what = $p[1]; window = $window; saying = "Starting $($p[2]) $how..." }
        $item.add_Click({
            param($clicked, $unused)
            $arguments = @('start', $clicked.Tag.what)
            if ($clicked.Tag.window) { $arguments += '--window' }
            Start-Control $arguments $clicked.Tag.saying
        })
    }
    $sub
}

$statusItems = @()
foreach ($i in 0..2) { $it = Add-Item '' $null; $it.Enabled = $false; $statusItems += $it }
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
$mapItem = Add-Item 'Open the map' { Start-Process "http://127.0.0.1:$($script:port)/map" }
$mapItem.Font = New-Object System.Drawing.Font($mapItem.Font, [System.Drawing.FontStyle]::Bold)
$speakersItem = Add-Item 'PC speakers' {
    $script:wantSpeakers = -not $script:wantSpeakers
    if ($script:wantSpeakers) { Remove-Item $speakersOffFile -ErrorAction SilentlyContinue }
    else { Set-Content $speakersOffFile 'PC speakers turned off from the tray icon'; Stop-AllSpeakers }
    Update-Speakers
}
$editItem = Add-Item 'Open SDRTrunk window (edit mode)' {
    if (Test-Path $editFlag) {
        [System.Windows.Forms.MessageBox]::Show('The SDRTrunk window is already open for edits.', 'SDRTrunk Plus') | Out-Null
    } else {
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$here\edit-mode.bat`"" -WorkingDirectory $here
    }
}
$windowsItem = Add-Item "Open the windows when I'm signed in" {
    $script:wantWindows = -not $script:wantWindows
    if ($script:wantWindows) { Remove-Item $windowsOffFile -ErrorAction SilentlyContinue; $script:windowsDone = $false }
    else { Set-Content $windowsOffFile 'Windows at sign-in turned off from the tray icon' }
}
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
Add-Item 'Restart everything' {
    if (-not $script:status) {
        [System.Windows.Forms.MessageBox]::Show('The watchdog is not running, so there is nothing to restart. Use Start > Everything in this menu to start it all.', 'SDRTrunk Plus') | Out-Null
        return
    }
    $answer = [System.Windows.Forms.MessageBox]::Show('Restart SDRTrunk, the transcriber and Ollama? Takes about a minute.', 'SDRTrunk Plus', 'YesNo', 'Question')
    if ($answer -eq 'Yes') { Set-Content $restartFlag 'asked from the tray icon' }
} | Out-Null
Add-Item 'Stop all' {
    $answer = [System.Windows.Forms.MessageBox]::Show(
        "Stop SDRTrunk, the transcriber, Ollama and the watchdog?`n`nNothing records or transcribes until you start it again (Start in this menu, or restart the PC). Takes up to two minutes.",
        'SDRTrunk Plus', 'YesNo', 'Question')
    if ($answer -eq 'Yes') { Start-Control @('stop-all') 'Stopping everything... the dot turns grey when done.' }
} | Out-Null
$startWindows = Add-StartMenu 'Start with windows' $true
Add-Item 'Update everything' {
    $updater = Find-Updater
    if (-not $updater) {
        [System.Windows.Forms.MessageBox]::Show('Could not find update-sdrtrunk-plus.bat. Run it from the sdrtrunk folder yourself.', 'SDRTrunk Plus') | Out-Null
        return
    }
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$updater`"" -WorkingDirectory (Split-Path $updater)
} | Out-Null
Add-Item 'Show the log' { Start-Process notepad.exe (Join-Path $here 'watchdog.log') } | Out-Null
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
Add-Item 'Close this icon (stops the PC speakers)' {
    $script:wantSpeakers = $false
    Update-Speakers
    $tray.Visible = $false
    [System.Windows.Forms.Application]::Exit()
} | Out-Null

$menu.add_Opening({
    Update-Tray
    $lines = Get-Lines $script:status
    for ($i = 0; $i -lt $statusItems.Count; $i++) {
        $statusItems[$i].Visible = $i -lt $lines.Count
        if ($i -lt $lines.Count) { $statusItems[$i].Text = $lines[$i] }
    }
    $speakersItem.Checked = $script:wantSpeakers
    $windowsItem.Checked = $script:wantWindows
    $editItem.Enabled = [bool]$script:status
})
$tray.add_DoubleClick({ Start-Process "http://127.0.0.1:$($script:port)/map" })

$jobTimer = New-Object System.Windows.Forms.Timer
$jobTimer.Interval = 1000
$jobTimer.add_Tick({ Show-ControlResult })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 10000
$timer.add_Tick({ Update-Tray })
$timer.Start()

$tray.Visible = $true
Stop-AllSpeakers  # a copy left by an earlier tray icon
Update-Tray
[System.Windows.Forms.Application]::Run()

$timer.Stop()
$jobTimer.Stop()
$tray.Dispose()
Stop-AllSpeakers
$mutex.ReleaseMutex()
