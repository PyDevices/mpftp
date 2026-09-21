# Install (or repair) the no-UAC ESP32 USB recovery: run this ONCE, elevated.
#
# Restarting a USB device node needs elevation, and an unattended session has
# nobody to click a UAC prompt. The way round that is a scheduled task running
# as SYSTEM which any account may start on demand -- the pattern
# docs/agent-guide.md describes. This script is the whole of the elevated half:
# it puts the recovery script somewhere only administrators can write, creates
# the unprivileged request directory beside it, and registers the task to run
# the one against the other.
#
# From an administrator PowerShell (mpftp#31):
#
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<this file>"
#
# It is idempotent -- run it again after editing restart-esp-usb.ps1, which is
# what a later fix costs. That price is deliberate: the task runs as SYSTEM, so
# nothing it executes may be writable by an ordinary account. The one thing an
# ordinary account writes is the request file, and that is data the recovery
# script matches against an allow-list, never code.
[CmdletBinding()]
param(
    [string]$TaskName = 'mpftp-restart-esp-usb',
    # Administrator-writable only. This is where the code goes.
    [string]$InstallDir = (Join-Path $env:ProgramFiles 'mpftp'),
    # User-writable. This is where the data goes.
    [string]$RequestDir = (Join-Path $env:ProgramData 'mpftp')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$source = Join-Path $PSScriptRoot 'restart-esp-usb.ps1'
$target = Join-Path $InstallDir 'restart-esp-usb.ps1'
$logPath = Join-Path $InstallDir 'restart-esp-usb.log'
$requestPath = Join-Path $RequestDir 'restart-esp-usb.target'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error ("This must run in an ADMINISTRATOR PowerShell -- it writes to $InstallDir and " +
        "registers a SYSTEM task, and both are refused at medium integrity. " +
        "Right-click PowerShell, Run as administrator, then run this file again.")
    exit 1
}
if (-not (Test-Path -LiteralPath $source)) {
    Write-Error "cannot find the recovery script next to this installer: $source"
    exit 1
}

function Set-ExplicitAcl {
    # Inheritance off, and exactly the three rules we mean. Stated rather than
    # inherited, so it does not quietly change when a parent directory does.
    param([string]$Path, [System.Security.AccessControl.FileSystemRights]$UsersRights)

    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    $inherit = 'ContainerInherit,ObjectInherit'
    foreach ($pair in @(
            @{ Sid = 'S-1-5-18';     Rights = [System.Security.AccessControl.FileSystemRights]::FullControl }  # SYSTEM
            @{ Sid = 'S-1-5-32-544'; Rights = [System.Security.AccessControl.FileSystemRights]::FullControl }  # Administrators
            @{ Sid = 'S-1-5-32-545'; Rights = $UsersRights }                                                   # Users
        )) {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
                    (New-Object System.Security.Principal.SecurityIdentifier $pair.Sid),
                    $pair.Rights, $inherit, 'None', 'Allow')))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

# ------------------------------------------------------- the code, admin-only

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Set-ExplicitAcl -Path $InstallDir -UsersRights ([System.Security.AccessControl.FileSystemRights]::ReadAndExecute)
Copy-Item -LiteralPath $source -Destination $target -Force
Write-Output "installed  $target  (Administrators/SYSTEM write, Users read+execute)"

if (-not (Test-Path -LiteralPath $logPath)) {
    New-Item -ItemType File -Force -Path $logPath | Out-Null
}
Write-Output "transcript $logPath  (SYSTEM appends, Users read -- the account that writes requests cannot rewrite the record of them)"

# -------------------------------------------------- the request, user-writable

New-Item -ItemType Directory -Force -Path $RequestDir | Out-Null
Set-ExplicitAcl -Path $RequestDir -UsersRights ([System.Security.AccessControl.FileSystemRights]::Modify)
Write-Output "requests   $requestPath  (Users write -- data only; one instance id, matched against an allow-list, never executed)"

# ------------------------------------------------------------------- the task

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $target)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$settings.AllowDemandStart = $true

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $taskPrincipal `
    -Settings $settings -Description (
    'mpftp#31: restart one Espressif USB device node named by ' + $requestPath +
    '. Recovers an ESP32-S3 wedged by machine.bootloader() without a UAC prompt.') -Force | Out-Null
Write-Output "registered task '$TaskName' -> $target"

# Let ordinary accounts start it, which is the entire point. This is the
# default for a SYSTEM task registered by an administrator; state it anyway, so
# the no-prompt path does not depend on a default staying put.
try {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $registered = $service.GetFolder('\').GetTask($TaskName)
    # Administrators and SYSTEM full control; Authenticated Users read + run.
    $registered.SetSecurityDescriptor('D:(A;;GA;;;BA)(A;;GA;;;SY)(A;;GRGX;;;AU)', 0)
    Write-Output "task security  Administrators/SYSTEM full, Authenticated Users read+run"
} catch {
    Write-Output "task security  left at the default (could not set it: $($_.Exception.Message))"
}

# ------------------------------------------------------------- prove the wiring

$registeredArgs = (Get-ScheduledTask -TaskName $TaskName).Actions[0].Arguments
if ($registeredArgs -notlike "*$target*") {
    Write-Error "the registered action does not point at $target -- it is: $registeredArgs"
    exit 1
}
if ($registeredArgs -like '*Restart-PnpDevice*') {
    Write-Error "the registered action still calls Restart-PnpDevice, which does not exist: $registeredArgs"
    exit 1
}

& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $target `
    -InstanceId 'not-an-instance-id' -LogPath $logPath -DryRun | Out-Null
if ($LASTEXITCODE -ne 3) {
    Write-Error "self-check failed: a malformed instance id should exit 3, got $LASTEXITCODE"
    exit 1
}
Write-Output "self-check  a malformed instance id is refused with exit 3"

Write-Output ''
Write-Output 'Done. Nothing else needs elevation. To prove it on a board, as an ordinary user:'
Write-Output ''
Write-Output '  mpftp usb-restart --status'
Write-Output '  mpftp usb-restart --instance "USB\VID_303A&PID_4003\<serial>"'
Write-Output ''
Write-Output "or without mpftp: write the instance id into $requestPath, then"
Write-Output "  schtasks /run /tn $TaskName"
Write-Output "  Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastTaskResult"
Write-Output "  Get-Content '$logPath' -Tail 20"
exit 0
