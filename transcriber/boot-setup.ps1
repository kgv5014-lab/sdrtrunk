# Used by boot-on.bat and boot-off.bat (run as administrator, which a boot task needs).
param([ValidateSet('on', 'off')][string]$Mode = 'on')

$ErrorActionPreference = 'Stop'
$home_ = $PSScriptRoot
$taskName = 'SDRTrunk Plus (boot)'
$startup = [Environment]::GetFolderPath('Startup')
$backup = Join-Path $home_ 'startup-backup'
$signInLink = Join-Path $startup 'SDRTrunk Plus.lnk'
$speakersLink = Join-Path $startup 'SDRTrunk Plus speakers.lnk'
$ollamaLink = Join-Path $startup 'Ollama.lnk'

function Finish($text, $color = 'Green') {
    Write-Host ''
    Write-Host $text -ForegroundColor $color
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit
}

if ($Mode -eq 'off') {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $speakersLink -ErrorAction SilentlyContinue
    if (Test-Path (Join-Path $backup 'Ollama.lnk')) { Move-Item (Join-Path $backup 'Ollama.lnk') $ollamaLink -Force }
    Finish ("Done. Nothing starts at boot any more; the next restart leaves everything off.`n" +
            "To start everything when you sign in instead, run autostart-on.bat. To start it now, run start-everything.bat.")
}

Write-Host 'SDRTrunk Plus: start at boot, before sign-in' -ForegroundColor Green
if (-not (Test-Path (Join-Path $home_ '.venv\Scripts\python.exe'))) { Finish 'Run install.bat in this folder first.' 'Red' }

$user = "$env:USERDOMAIN\$env:USERNAME"
Write-Host ''
Write-Host "Windows needs your password to run things as $user while nobody is signed in."
Write-Host 'Use your Windows sign-in password (for a Microsoft account, that account''s password, not the PIN).'
Write-Host 'It is kept by Windows Task Scheduler, not by SDRTrunk Plus.'
$secure = Read-Host 'Password' -AsSecureString
$password = [System.Net.NetworkCredential]::new('', $secure).Password

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$home_\boot-start.bat`"" -WorkingDirectory $home_
$trigger = New-ScheduledTaskTrigger -AtStartup
# A minute for the USB dongles, network, Tailscale and NordVPN to come up.
$trigger.Delay = 'PT1M'
# No time limit (Windows' default stops a task after 3 days), normal priority (the default is below
# normal, which would slow decoding), and start again if it ever stops.
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 4 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -User $user -Password $password -RunLevel Limited -Force `
        -Description 'Starts SDRTrunk, the transcriber and Ollama at boot with no windows (boot-on.bat)' | Out-Null
} catch {
    Finish "Could not add the boot task: $($_.Exception.Message)`nIf it says the password is wrong, run boot-on.bat again." 'Red'
}
Write-Host 'Added the boot task.'

New-Item -ItemType Directory -Force -Path $backup | Out-Null
# The sign-in autostart (autostart-on.bat) isn't needed any more.
Remove-Item $signInLink -ErrorAction SilentlyContinue
# Ollama's own tray app starts a second copy at sign-in, on the wrong GPU. Put it aside (boot-off.bat puts it back).
if (Test-Path $ollamaLink) { Move-Item $ollamaLink (Join-Path $backup 'Ollama.lnk') -Force; Write-Host 'Turned off Ollama''s own start at sign-in.' }

# Sound: Windows only gives speakers to someone signed in, so a small player starts at sign-in.
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($speakersLink)
$link.TargetPath = Join-Path $home_ 'start-speakers.bat'
$link.WorkingDirectory = $home_
$link.WindowStyle = 7
$link.Description = 'Plays the live calls on the PC speakers'
$link.Save()
Write-Host 'Added the PC speakers player, which starts when you sign in.'

Finish ("Done. Restart the PC: everything starts by itself about a minute after boot, signed in or not.`n" +
        "Sound plays on the speakers once you sign in. To change channels or settings, run edit-mode.bat.`n" +
        "To go back to the normal way, run boot-off.bat.")
