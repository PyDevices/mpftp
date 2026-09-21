# Test doubles for tools/windows/restart-esp-usb.ps1 (mpftp#31).
#
# The part of that script which touches a device cannot be exercised for real
# by an ordinary account, and should not be exercised for real by a test at
# all. But what it does to a device is a handful of calls -- Get-PnpDevice,
# pnputil.exe, Disable-/Enable-PnpDevice -- and PowerShell resolves a function
# before a cmdlet or an executable of the same name. So this file defines
# those names, records every call, and dot-sources the REAL script underneath
# them. The script's own text runs; only the hardware is pretend.
#
# Scenarios:
#   healthy       a present node, pnputil restarts it
#   disabled      the node is CM_PROB_DISABLED when the script finds it
#   gone-away     pnputil has /restart-device and answers 1167 "not connected"
#   old-windows   pnputil has no /restart-device; Disable works, Enable throws
param(
    [Parameter(Mandatory = $true)][string]$Script,
    [Parameter(Mandatory = $true)][string]$Scenario,
    [Parameter(Mandatory = $true)][string]$Calls,
    [Parameter(Mandatory = $true)][string]$Log
)

$script:DoubleCalls = $Calls
$script:DoubleScenario = $Scenario
$script:DoubleProblem = if ($Scenario -eq 'disabled') { 'CM_PROB_DISABLED' } else { 'CM_PROB_NONE' }

function Note-Call {
    param([string]$What)
    Add-Content -LiteralPath $script:DoubleCalls -Value $What -Encoding UTF8
}

function Get-PnpDevice {
    [pscustomobject]@{
        FriendlyName = 'a test double'
        Status       = if ($script:DoubleProblem -eq 'CM_PROB_NONE') { 'OK' } else { 'Error' }
        Problem      = $script:DoubleProblem
    }
}

function Disable-PnpDevice {
    Note-Call 'Disable-PnpDevice'
    $script:DoubleProblem = 'CM_PROB_DISABLED'
}

function Enable-PnpDevice {
    Note-Call 'Enable-PnpDevice'
    if ($script:DoubleScenario -eq 'old-windows') {
        throw 'The device is not connected.'
    }
    $script:DoubleProblem = 'CM_PROB_NONE'
}

function Start-Sleep { }

function pnputil.exe {
    # `& pnputil.exe @('/verb', $id)` hands an executable two arguments and a
    # function one array, so flatten before reading the verb.
    $flat = @($args | ForEach-Object { $_ })
    $verb = "$($flat[0])"
    Note-Call ('pnputil ' + $verb)
    $global:LASTEXITCODE = 0
    switch ($verb) {
        '/?' {
            if ($script:DoubleScenario -eq 'old-windows') {
                'PNPUTIL /add-driver /delete-driver /enum-devices /enable-device /disable-device'
            } else {
                'PNPUTIL /add-driver /delete-driver /enum-devices /enable-device /disable-device /restart-device'
            }
        }
        '/enable-device' {
            $script:DoubleProblem = 'CM_PROB_NONE'
            'Device enabled successfully.'
        }
        '/restart-device' {
            if ($script:DoubleScenario -eq 'gone-away' -or $script:DoubleScenario -eq 'old-windows') {
                $global:LASTEXITCODE = 1167
                'Failed to restart device. The device is not connected.'
            } else {
                'Device restarted successfully.'
            }
        }
    }
}

. $Script -InstanceId 'USB\VID_303A&PID_4003\D0UB1E0000000000' -LogPath $Log
# `exit` in a dot-sourced script returns here, with its code in $LASTEXITCODE.
exit $LASTEXITCODE
