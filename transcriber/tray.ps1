# The SDRTrunk Plus tray icon, started when you sign in (boot-on.bat adds it to Startup).
#
# The dot by the clock shows how the background programs are doing, from watchdog-status.json:
#   green = all running, orange = starting, edit mode or updating, red = one stopped, grey = no watchdog.
# Right-click it for the map, edit mode, the PC speakers, restart, update and the log.
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
    if (-not $s) { return @('The watchdog is not running') }
    @($s.programs | ForEach-Object {
        $n = $names[$_.name]; if (-not $n) { $n = $_.name }
        $w = $words[$_.state]; if (-not $w) { $w = $_.state }
        "${n}: $w"
    })
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
        $script:speakers = $null
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
    Update-Speakers
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

$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = $icons.off
$tray.Text = 'SDRTrunk Plus'
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$tray.ContextMenuStrip = $menu

function Add-Item($text, [scriptblock]$action) {
    $item = New-Object System.Windows.Forms.ToolStripMenuItem $text
    if ($action) { $item.add_Click($action) }
    [void]$menu.Items.Add($item)
    $item
}

$statusItems = @()
foreach ($i in 0..2) { $it = Add-Item '' $null; $it.Enabled = $false; $statusItems += $it }
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
$mapItem = Add-Item 'Open the map' { Start-Process "http://127.0.0.1:$($script:port)/map" }
$mapItem.Font = New-Object System.Drawing.Font($mapItem.Font, [System.Drawing.FontStyle]::Bold)
$speakersItem = Add-Item 'PC speakers' {
    $script:wantSpeakers = -not $script:wantSpeakers
    if ($script:wantSpeakers) { Remove-Item $speakersOffFile -ErrorAction SilentlyContinue }
    else { Set-Content $speakersOffFile 'PC speakers turned off from the tray icon' }
    Update-Speakers
}
$editItem = Add-Item 'Open SDRTrunk window (edit mode)' {
    if (Test-Path $editFlag) {
        [System.Windows.Forms.MessageBox]::Show('The SDRTrunk window is already open for edits.', 'SDRTrunk Plus') | Out-Null
    } else {
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$here\edit-mode.bat`"" -WorkingDirectory $here
    }
}
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
Add-Item 'Restart everything' {
    if (-not $script:status) {
        [System.Windows.Forms.MessageBox]::Show('The watchdog is not running, so there is nothing to restart. Restart the PC to start everything.', 'SDRTrunk Plus') | Out-Null
        return
    }
    $answer = [System.Windows.Forms.MessageBox]::Show('Restart SDRTrunk, the transcriber and Ollama? Takes about a minute.', 'SDRTrunk Plus', 'YesNo', 'Question')
    if ($answer -eq 'Yes') { Set-Content $restartFlag 'asked from the tray icon' }
} | Out-Null
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
    $editItem.Enabled = [bool]$script:status
})
$tray.add_DoubleClick({ Start-Process "http://127.0.0.1:$($script:port)/map" })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 10000
$timer.add_Tick({ Update-Tray })
$timer.Start()

$tray.Visible = $true
Update-Tray
[System.Windows.Forms.Application]::Run()

$timer.Stop()
$tray.Dispose()
if ($script:speakers -and -not $script:speakers.HasExited) { Stop-Process -Id $script:speakers.Id -Force -ErrorAction SilentlyContinue }
$mutex.ReleaseMutex()
