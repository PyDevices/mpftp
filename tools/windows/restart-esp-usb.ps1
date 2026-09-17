# Restart the USB device node of any attached Espressif board (VID 303A).
#
# This is the repair for the ESP32-S3 native-USB wedge described in
# docs/agent-guide.md: after `mpftp bootloader` or a direct
# `machine.bootloader()`, Windows goes on reporting a healthy device while
# every attempt to open its COM port fails as "busy or locked", with no
# process holding the handle. Restarting the device node makes Windows
# enumerate what the chip is really presenting -- a board sitting in ROM
# download mode then appears as 303A:1001 on a new COM number.
#
# Needs elevation. From an ordinary shell (WSL included), this pops one UAC
# prompt and grants nothing that outlives it:
#
#   powershell.exe -NoProfile -Command "Start-Process powershell -Verb RunAs `
#     -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<this file>'"
#
# **There is no `Restart-PnpDevice`.** Windows PowerShell's PnpDevice module
# ships Get-, Enable- and Disable-PnpDevice and nothing else, so a script that
# calls Restart-PnpDevice fails with CommandNotFoundException on every device
# and exits 1 -- while a scheduled task wrapping it reports LastTaskResult 1
# and looks, from the outside, exactly like a board that refused to restart.
# Found the expensive way on 2026-09-17, mid flash cycle. `pnputil
# /restart-device` is the real mechanism; Disable+Enable is the fallback for a
# Windows older than 2004.
[CmdletBinding()]
param(
    # Espressif's vendor ID. Narrow it (e.g. 'USB\VID_303A&PID_4001*') when two
    # boards are attached and only one should be restarted.
    [string]$Match = 'USB\VID_303A*'
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output "not elevated -- every restart below will fail with Access is denied"
}

# Restart the composite parent, not its MI_ children: re-enumerating the
# parent brings the interfaces with it, and a child whose parent is about to
# vanish reports a confusing failure of its own.
$devices = Get-PnpDevice -PresentOnly |
    Where-Object { $_.InstanceId -like $Match -and $_.InstanceId -notmatch '&MI_' }
if (-not $devices) {
    Write-Output "no device matching $Match is attached"
    exit 1
}

$failed = 0
foreach ($d in $devices) {
    Write-Output ("restarting {0}  [{1}]" -f $d.InstanceId, $d.Status)
    $out = & pnputil.exe /restart-device $d.InstanceId 2>&1
    if ($LASTEXITCODE -eq 0 -and ($out -join ' ') -notmatch 'Failed to restart') {
        Write-Output "  ok (pnputil)"
        continue
    }
    Write-Output ("  pnputil: {0}" -f (($out | Where-Object { $_ -match '\S' }) -join '; '))
    try {
        Disable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction Stop
        Start-Sleep -Milliseconds 700
        Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction Stop
        Write-Output "  ok (disable/enable)"
    } catch {
        Write-Output ("  failed: {0}" -f $_.Exception.Message)
        $failed++
    }
}

Start-Sleep -Seconds 3
Get-PnpDevice -PresentOnly |
    Where-Object { $_.InstanceId -like 'USB\VID_303A*' } |
    Select-Object Status, InstanceId |
    Format-Table -AutoSize | Out-String | Write-Output

exit $(if ($failed) { 1 } else { 0 })
