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
[CmdletBinding()]
param(
    # Espressif's vendor ID. Narrow it (e.g. 'USB\VID_303A&PID_4001*') when two
    # boards are attached and only one should be restarted.
    [string]$Match = 'USB\VID_303A*'
)

$devices = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like $Match }
if (-not $devices) {
    Write-Output "no device matching $Match is attached"
    exit 1
}
foreach ($d in $devices) {
    Write-Output ("restarting {0}  [{1}]" -f $d.InstanceId, $d.Status)
    try {
        Restart-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction Stop
        Write-Output "  ok"
    } catch {
        Write-Output ("  failed: {0}" -f $_.Exception.Message)
    }
}
Start-Sleep -Seconds 3
Get-PnpDevice -PresentOnly |
    Where-Object { $_.InstanceId -like 'USB\VID_303A*' } |
    Select-Object Status, InstanceId |
    Format-Table -AutoSize | Out-String | Write-Output
