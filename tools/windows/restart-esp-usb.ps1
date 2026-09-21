# Restart one Espressif USB device node, named by instance id.
#
# This is the repair for the ESP32-S3 native-USB wedge described in
# docs/agent-guide.md: after `mpftp bootloader` or a direct
# `machine.bootloader()`, the firmware deletes its own USB PHY and reboots, and
# Windows can be left serving a node the chip no longer presents. Every open
# then fails -- "busy or locked", error 31, no process holding the handle --
# until the node is re-enumerated. Restarting it makes Windows read what the
# chip is really presenting; a board sitting in ROM download mode then appears
# as 303A:1001 on a new COM number.
#
# Restarting a device node needs elevation, so unattended sessions reach this
# script through the scheduled task `mpftp-restart-esp-usb`, which runs it as
# SYSTEM and which any account may start on demand without a UAC prompt.
# Register it with tools/windows/install-restart-esp-usb-task.ps1, once, from an
# administrator PowerShell.
#
# ## The rules that keep a SYSTEM task from being a back door
#
# A task running as SYSTEM must never execute anything an ordinary user can
# write, so this script lives beside the task in an administrator-only
# directory (C:\Program Files\mpftp), and changing it costs another elevated
# install. That is the correct price.
#
# What an ordinary user may write is the *request file*, and it is data, never
# code. One line, matched whole against a strict allow-list for an Espressif
# instance id, never evaluated, never concatenated into a command string --
# pnputil is called with an argument array. Anything else is refused with a
# non-zero exit and a logged sentence. The request is consumed (deleted) on
# read, so a rejected or stale one is never replayed.
#
# One device per run, by explicit id. The predecessor matched `USB\VID_303A*`
# and would have bounced every attached Espressif board together -- on this
# bench that is a second board mid-demo.
#
# ## There is no Restart-PnpDevice
#
# Windows PowerShell's PnpDevice module ships Get-, Enable- and Disable-PnpDevice
# and nothing else, so a script that calls Restart-PnpDevice fails with
# CommandNotFoundException on every device and exits 1 -- while a scheduled task
# wrapping it reports LastTaskResult 1 and looks, from the outside, exactly like
# a board that refused to restart. That is what the task registered here
# actually ran, from whenever it was created until 2026-09-21, and it is why
# mpftp#31 exists. `pnputil /restart-device` is the mechanism that works;
# Disable+Enable is the fallback for a Windows older than 10 2004.
#
# Exit codes are the whole report when this runs as a task: 0 restarted (or,
# under -DryRun, would have), 2 no request, 3 request refused, 4 no such device
# attached, 5 the restart itself failed.
[CmdletBinding()]
param(
    # The device to restart. Omitted -- which is how the scheduled task runs --
    # it is read from -RequestPath instead.
    [string]$InstanceId,

    # Where an unprivileged caller leaves the instance id. Deleted on read.
    [string]$RequestPath = (Join-Path $env:ProgramData 'mpftp\restart-esp-usb.target'),

    # Appended to, never truncated. Lives in the admin-only directory so the
    # account that writes requests cannot rewrite the record of them.
    # Resolved below, NOT here: under `powershell.exe -File` -- which is how the
    # scheduled task runs this -- $PSScriptRoot is still empty while the param
    # block's defaults are evaluated (Windows PowerShell 5.1), so a default of
    # `Join-Path $PSScriptRoot ...` throws before the first log line and the
    # task exits 1 having done nothing. That is exactly what the first install
    # did on 2026-09-21; every test had passed -LogPath explicitly.
    [string]$LogPath = '',

    # Validate, resolve and report, but do not touch the device. Everything up
    # to the restart is read-only, so this is the part that can be tested by an
    # ordinary user with a board in use nearby.
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $LogPath) {
    # In the script body $PSScriptRoot is populated under -File as well as
    # under `& script`; the fallbacks are for a host that sets neither.
    $here = $PSScriptRoot
    if (-not $here -and $PSCommandPath) { $here = Split-Path -Parent $PSCommandPath }
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $LogPath = Join-Path $here 'restart-esp-usb.log'
}

# Espressif's vendor ID, a 4-hex-digit product id, and a serial of the
# characters Windows actually puts there. Anchored both ends, so a trailing
# `; calc` or a second path component cannot ride along. The `\\` after the
# product id is deliberate: it admits the composite *parent* and rejects its
# `&MI_00` children -- re-enumerating the parent brings the interfaces with it,
# and a child whose parent is about to vanish reports a confusing failure of
# its own.
$ALLOWED_INSTANCE_ID = '^USB\\VID_303A&PID_[0-9A-Fa-f]{4}\\[0-9A-Za-z&_.\-]+$'
$MAX_REQUEST_CHARS = 200

function Write-Log {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'), $Message
    Write-Output $line
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        # A task with nowhere to write its transcript still has its exit code,
        # which is the thing the caller reads first. Do not fail the repair.
        Write-Output ('{0} (transcript unavailable: {1})' -f $line, $_.Exception.Message)
    }
}

function Test-UnsafeRequestPath {
    # SYSTEM reads and deletes this path, and an ordinary account owns the
    # directory it sits in -- so refuse anything that could redirect either
    # operation somewhere else. Returns a sentence to log, or $null if safe.
    #
    # Both conditions below are load-bearing, and neither subsumes the other.
    # Measured on this bench, all three creatable by an ordinary user:
    #   a junction      Attributes: Directory, ReparsePoint   LinkType: Junction
    #   a WSL symlink   Attributes: Archive, ReparsePoint     LinkType: (blank)
    #   a hard link     Attributes: Archive                   LinkType: HardLink
    # The attribute alone misses the hard link; LinkType alone misses the
    # symlink. A junction needs no privilege at all to create.
    param([string]$Path)

    $reparse = [IO.FileAttributes]::ReparsePoint
    $parent = Split-Path -Parent $Path
    if ($parent) {
        try {
            $dir = Get-Item -LiteralPath $parent -Force -ErrorAction Stop
            if ($dir.Attributes -band $reparse) {
                return 'sits in a directory that is a reparse point (a junction or mount point), which would redirect this read somewhere else.'
            }
        } catch {
            return ('sits in a directory that cannot be inspected: {0}' -f $_.Exception.Message)
        }
    }

    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch {
        return ('cannot be inspected: {0}' -f $_.Exception.Message)
    }
    if ($item -is [System.IO.DirectoryInfo]) {
        return 'is a directory, not a file.'
    }
    if ($item.Attributes -band $reparse) {
        return 'is a reparse point (a symbolic link), not a regular file.'
    }
    $linkType = $null
    try { $linkType = $item.LinkType } catch { $linkType = $null }
    if (-not [string]::IsNullOrEmpty($linkType)) {
        return ('is a {0}, not a regular file.' -f $linkType)
    }
    return $null
}

function Show-Refused {
    # Echo a refused request back to the log without letting it forge log lines
    # or blow the file up: control characters out, length capped.
    param([string]$Text)
    if ($null -eq $Text) { return '<null>' }
    $flat = ($Text -replace '[\x00-\x1F\x7F]', '?')
    if ($flat.Length -gt $MAX_REQUEST_CHARS) {
        $flat = $flat.Substring(0, $MAX_REQUEST_CHARS) + '...'
    }
    return "'" + $flat + "'"
}

Write-Log ('--- restart-esp-usb starting (dryRun={0}, user={1}) ---' -f `
    [bool]$DryRun, [Security.Principal.WindowsIdentity]::GetCurrent().Name)

# ---------------------------------------------------------------- the request

if (-not $InstanceId) {
    if (-not (Test-Path -LiteralPath $RequestPath)) {
        Write-Log ('no request: {0} does not exist. Nothing to restart.' -f $RequestPath)
        exit 2
    }
    # Before the read, and before the delete. Nothing about a refused path is
    # echoed but the path itself -- with Developer Mode on, an ordinary user can
    # make this a link to a file only SYSTEM can read, and this log is readable
    # by everyone.
    $unsafe = Test-UnsafeRequestPath -Path $RequestPath
    if ($unsafe) {
        Write-Log ('request refused: {0} {1} Nothing was read and nothing was deleted.' -f $RequestPath, $unsafe)
        exit 3
    }

    $raw = $null
    try {
        $raw = Get-Content -LiteralPath $RequestPath -Raw -ErrorAction Stop
    } catch {
        Write-Log ('request unreadable: {0}' -f $_.Exception.Message)
        exit 2
    }

    # Re-checked immediately before the delete. This does not close the gap
    # between check and syscall -- doing that needs an open handle with
    # FILE_FLAG_OPEN_REPARSE_POINT, which is beyond PowerShell -- but it is the
    # delete that is the dangerous half, so it is worth narrowing.
    $unsafe = Test-UnsafeRequestPath -Path $RequestPath
    if ($unsafe) {
        Write-Log ('request refused between read and delete: {0} {1} Nothing was deleted.' -f $RequestPath, $unsafe)
        exit 3
    }
    # Consume it. A request that is left behind gets replayed by the next run
    # of the task, which is somebody else's device.
    try { Remove-Item -LiteralPath $RequestPath -Force -ErrorAction Stop }
    catch { Write-Log ('warning: could not delete {0}: {1}' -f $RequestPath, $_.Exception.Message) }

    if ([string]::IsNullOrWhiteSpace($raw)) {
        Write-Log ('request refused: {0} is empty.' -f $RequestPath)
        exit 2
    }
    if ($raw.Length -gt $MAX_REQUEST_CHARS) {
        Write-Log ('request refused: {0} chars, limit is {1}.' -f $raw.Length, $MAX_REQUEST_CHARS)
        exit 3
    }
    $lines = @($raw -split "`r`n|`n|`r" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lines.Count -ne 1) {
        Write-Log ('request refused: expected exactly one line, got {0}. One device per run.' -f $lines.Count)
        exit 3
    }
    $InstanceId = $lines[0].Trim()
}

if ($InstanceId -notmatch $ALLOWED_INSTANCE_ID) {
    Write-Log ('request refused: {0} is not an Espressif USB instance id. It must match {1} -- the composite parent of a VID_303A device, not an &MI_ child and not anything else.' -f `
        (Show-Refused $InstanceId), $ALLOWED_INSTANCE_ID)
    exit 3
}
Write-Log ('target {0}' -f $InstanceId)

# ---------------------------------------------------------------- the device

$device = $null
try {
    $device = Get-PnpDevice -PresentOnly -InstanceId $InstanceId -ErrorAction Stop
} catch {
    $device = $null
}
if (-not $device) {
    Write-Log ('no device with instance id {0} is attached. Refusing to report success for a board that is not there.' -f $InstanceId)
    exit 4
}
Write-Log ('found "{0}" status={1}' -f $device.FriendlyName, $device.Status)

if ($DryRun) {
    Write-Log 'dry run: would restart it now. Stopping here without touching the device.'
    exit 0
}

# ---------------------------------------------------------------- the restart

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Log 'not elevated -- the restart below will fail with "Access is denied". Run this through the mpftp-restart-esp-usb scheduled task, which runs as SYSTEM.'
}

$restarted = $false
# An argument array, never a command string: the id reached this line as data
# and it stays data.
$pnputilArgs = @('/restart-device', $InstanceId)
$out = & pnputil.exe @pnputilArgs 2>&1
$text = (($out | Where-Object { "$_" -match '\S' }) -join '; ')
if ($LASTEXITCODE -eq 0 -and $text -notmatch 'Failed to restart') {
    Write-Log ('pnputil /restart-device ok: {0}' -f $text)
    $restarted = $true
} else {
    Write-Log ('pnputil /restart-device failed (exit {0}): {1}' -f $LASTEXITCODE, $text)
    try {
        Disable-PnpDevice -InstanceId $InstanceId -Confirm:$false -ErrorAction Stop
        Start-Sleep -Milliseconds 700
        Enable-PnpDevice -InstanceId $InstanceId -Confirm:$false -ErrorAction Stop
        Write-Log 'disable/enable fallback ok'
        $restarted = $true
    } catch {
        Write-Log ('disable/enable fallback failed: {0}' -f $_.Exception.Message)
    }
}

if (-not $restarted) {
    Write-Log 'restart FAILED.'
    exit 5
}

Start-Sleep -Seconds 3
$after = Get-PnpDevice -PresentOnly -InstanceId $InstanceId -ErrorAction SilentlyContinue
if ($after) {
    Write-Log ('back as "{0}" status={1}' -f $after.FriendlyName, $after.Status)
} else {
    # Expected, and not a failure: a board that entered ROM download mode
    # re-enumerates as a different device (303A:1001 on a new COM number).
    Write-Log 'that instance id is gone -- the board has re-enumerated as a different device. Look for 303A:1001 on a new COM number.'
}
Write-Log '--- restart-esp-usb done ---'
exit 0
