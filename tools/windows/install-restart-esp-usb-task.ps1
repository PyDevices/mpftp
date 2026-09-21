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

$RIGHTS = [System.Security.AccessControl.FileSystemRights]
$SID_SYSTEM = 'S-1-5-18'
$SID_ADMINS = 'S-1-5-32-544'
$SID_USERS = 'S-1-5-32-545'

function New-Sid { param([string]$Value) New-Object System.Security.Principal.SecurityIdentifier $Value }

function New-Rule {
    param([string]$Sid, $Rights, [string]$Inherit = 'None', [string]$Propagation = 'None')
    New-Object System.Security.AccessControl.FileSystemAccessRule(
        (New-Sid $Sid), $Rights, $Inherit, $Propagation, 'Allow')
}

function Clear-InheritedAcl {
    # Inheritance off and every existing rule dropped, so what follows is
    # exactly what we mean rather than whatever a parent directory grants.
    param($Acl)
    $Acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($Acl.Access)) { [void]$Acl.RemoveAccessRuleSpecific($rule) }
    return $Acl
}

function Set-AdminOnlyAcl {
    # For the directory that holds code, and for the placeholder file. Users may
    # look; only SYSTEM and administrators may write.
    param([string]$Path, [switch]$NoInherit)

    $acl = Clear-InheritedAcl (Get-Acl -LiteralPath $Path)
    $inherit = if ($NoInherit) { 'None' } else { 'ContainerInherit,ObjectInherit' }
    $acl.AddAccessRule((New-Rule $SID_SYSTEM $RIGHTS::FullControl $inherit))
    $acl.AddAccessRule((New-Rule $SID_ADMINS $RIGHTS::FullControl $inherit))
    if (-not $NoInherit) {
        $acl.AddAccessRule((New-Rule $SID_USERS $RIGHTS::ReadAndExecute $inherit))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Set-RequestDirAcl {
    # The one directory an ordinary account writes to -- and the one SYSTEM
    # reads and deletes from, which is what makes its permissions matter.
    #
    # Users get exactly enough to leave a request and manage their own: add a
    # file to this folder, and Modify on files inside it. They do NOT get Delete
    # on the folder itself, nor CreateDirectories, nor WriteAttributes, nor
    # ChangePermissions. So the directory cannot be removed and recreated as a
    # junction pointing at \RPC Control or anywhere else -- which is the whole
    # reason for spelling this out rather than granting Modify and moving on.
    param([string]$Path)

    $acl = Clear-InheritedAcl (Get-Acl -LiteralPath $Path)
    $acl.AddAccessRule((New-Rule $SID_SYSTEM $RIGHTS::FullControl 'ContainerInherit,ObjectInherit'))
    $acl.AddAccessRule((New-Rule $SID_ADMINS $RIGHTS::FullControl 'ContainerInherit,ObjectInherit'))
    # This folder only: look at it, and add a file to it. Nothing else.
    $acl.AddAccessRule((New-Rule $SID_USERS ($RIGHTS::ReadAndExecute -bor $RIGHTS::CreateFiles) 'None' 'None'))
    # Files inside it, inherit-only: write and remove your own request.
    $acl.AddAccessRule((New-Rule $SID_USERS $RIGHTS::Modify 'ObjectInherit' 'InheritOnly'))
    Set-Acl -LiteralPath $Path -AclObject $acl

    # An owner always keeps the implicit right to rewrite the permissions above,
    # so the directory must not be owned by the account it constrains.
    try {
        $owner = Get-Acl -LiteralPath $Path
        $owner.SetOwner((New-Sid $SID_ADMINS))
        # Explicit, because Set-Acl reports this one as a non-terminating error
        # and the catch below would otherwise never run.
        Set-Acl -LiteralPath $Path -AclObject $owner -ErrorAction Stop
    } catch {
        Write-Output ("  note: could not set the owner to Administrators: " + $_.Exception.Message)
    }
}

# ------------------------------------------------------- the code, admin-only

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Set-AdminOnlyAcl -Path $InstallDir
Copy-Item -LiteralPath $source -Destination $target -Force
Write-Output "installed  $target  (Administrators/SYSTEM write, Users read+execute)"

if (-not (Test-Path -LiteralPath $logPath)) {
    New-Item -ItemType File -Force -Path $logPath | Out-Null
}
Write-Output "transcript $logPath  (SYSTEM appends, Users read -- the account that writes requests cannot rewrite the record of them)"

# -------------------------------------------------- the request, user-writable

if (Test-Path -LiteralPath $RequestDir) {
    # It may already have been replaced by something that redirects SYSTEM's
    # read and delete elsewhere. Do not install onto that.
    $existing = Get-Item -LiteralPath $RequestDir -Force
    if ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        Write-Error ("$RequestDir is a reparse point (a junction or mount point), not a real directory. " +
            "Refusing to install onto it. Inspect it, remove it by hand, and run this again.")
        exit 1
    }
} else {
    New-Item -ItemType Directory -Force -Path $RequestDir | Out-Null
}
Set-RequestDirAcl -Path $RequestDir

# A directory with anything at all in it cannot be converted into a reparse
# point, and an ordinary account cannot remove this file. Together with the
# absent Delete on the directory, that is what keeps the path a real directory.
$keep = Join-Path $RequestDir '.keep'
Set-Content -LiteralPath $keep -Encoding UTF8 -Value @(
    'Keeps this directory non-empty, so it cannot be converted into a junction.',
    'An ordinary account cannot delete it. Do not remove it. mpftp#31.')
Set-AdminOnlyAcl -Path $keep -NoInherit

Write-Output "requests   $requestPath  (Users may add a file here and rewrite their own; not delete the folder, not make one)"
Write-Output "            $keep  (SYSTEM/Administrators only -- keeps the folder non-empty)"

# ------------------------------------------------------------------- the task

# -LogPath is passed explicitly as well as defaulted inside the script: the
# first install (2026-09-21) registered a task whose script died in its own
# param block under -File, and said nothing. Belt and braces.
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -LogPath "{1}"' -f $target, $logPath)
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

# Run it the way the TASK runs it: -File, and NO -LogPath, so the script's own
# default is what gets exercised. Passing -LogPath here is what hid the
# param-block failure from the first install's self-check.
& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $target `
    -InstanceId 'not-an-instance-id' -DryRun | Out-Null
if ($LASTEXITCODE -ne 3) {
    Write-Error "self-check failed: a malformed instance id should exit 3, got $LASTEXITCODE"
    exit 1
}
Write-Output "self-check  a malformed instance id is refused with exit 3"

if (-not (Test-Path -LiteralPath $keep)) {
    Write-Error "self-check failed: $keep is missing, so the request directory could be emptied and converted into a junction"
    exit 1
}
if ((Get-Item -LiteralPath $RequestDir -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    Write-Error "self-check failed: $RequestDir is a reparse point"
    exit 1
}
Write-Output "self-check  the request directory is a real directory and cannot be emptied"

# Print what Users actually ended up with, so the claim above is checkable
# rather than asserted.
Write-Output ''
Write-Output "what BUILTIN\Users may do in $RequestDir :"
(Get-Acl -LiteralPath $RequestDir).Access |
    Where-Object { $_.IdentityReference -match 'Users$' } |
    ForEach-Object {
    Write-Output ('  {0}  [inherit {1}, propagate {2}]' -f $_.FileSystemRights, $_.InheritanceFlags, $_.PropagationFlags)
}

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
