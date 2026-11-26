<#
.SYNOPSIS
    Create (or remove) Windows shortcuts that start agent-monitor in the tray.

.DESCRIPTION
    Creates "agent-monitor" in the Start Menu and on the desktop. Each one
    runs the tray launcher from this repository's virtualenv, with no console
    window, and starts in the repository folder so the server uses the same
    database (data/agent_monitor.db) and .env as when you run it by hand.

    Run it again after moving the repository.

    Requires the tray extra:  .venv\Scripts\python.exe -m pip install -e ".[tray]"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1 -NoDesktop
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1 -Remove
#>
param(
    [switch]$NoDesktop,  # Start Menu only
    [switch]$Remove      # delete the shortcuts and the icon instead
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
# pythonw.exe: Python without a console window. The shortcut runs it rather
# than pip's agent-monitor-tray.exe, which is unsigned and which Windows
# security policies may refuse to start ("access denied").
$pythonw = Join-Path $repo '.venv\Scripts\pythonw.exe'
$iconDir = Join-Path $env:LOCALAPPDATA 'agent-monitor'
$icon = Join-Path $iconDir 'agent-monitor.ico'

$folders = @([Environment]::GetFolderPath('Programs'))  # Start Menu > Programs
if (-not $NoDesktop -or $Remove) { $folders += [Environment]::GetFolderPath('Desktop') }
$shortcuts = $folders | ForEach-Object { Join-Path $_ 'agent-monitor.lnk' }

if ($Remove) {
    foreach ($path in $shortcuts + $icon) {
        if (Test-Path $path) {
            Remove-Item $path
            Write-Host "Removed $path"
        }
    }
    if ((Test-Path $iconDir) -and -not (Get-ChildItem $iconDir)) { Remove-Item $iconDir }
    return
}

if (-not (Test-Path $pythonw)) {
    throw "Virtualenv not found: $pythonw`nCreate it first (see the README's Quick start)."
}

& $python -c "import pystray, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The tray extra is missing. Install it first: .venv\Scripts\python.exe -m pip install -e "".[tray]"""
}

& $python -m agent_monitor.tray --write-icon $icon
if ($LASTEXITCODE -ne 0) { throw "Could not write the icon to $icon" }

$shell = New-Object -ComObject WScript.Shell
foreach ($path in $shortcuts) {
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '-m agent_monitor.tray'
    $shortcut.WorkingDirectory = $repo
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = 'Real-time monitor for Claude Code agents'
    $shortcut.Save()
    Write-Host "Created $path"
}

# Ask Explorer to refresh its icon cache, so a re-created icon shows at once.
$refresh = Join-Path $env:SystemRoot 'System32\ie4uinit.exe'
if (Test-Path $refresh) { & $refresh -show }
