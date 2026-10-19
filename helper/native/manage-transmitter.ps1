# co-Link separate local executor. Cloud selects the backend; signed plan required for Convert.
# Audio co-DAC transmitter driver helper.
# UTF-8 with BOM is required because PowerShell 5.1 reads this file as the ANSI code page
# otherwise; Korean Windows would then corrupt the Korean error text below.
# 한국어 Windows에서도 오류 문구와 장치 이름이 깨지지 않도록 UTF-8 BOM으로 저장합니다.
#
# Modes:
#   List     read-only inventory of present USB Bluetooth HCI radios (no administrator token)
#   Check    read-only selected-device USB configuration validation (no administrator token)
#   Convert  bind exactly one selected Bluetooth HCI radio to a generated WinUSB package
#   Restore  bind exactly one selected radio back to its recorded original driver INF
#
# This helper never stops a service, never changes boot/security/test-signing policy, never
# reboots, and never deletes a driver package. The dialog stops the radio session before it
# launches a mutation.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('List', 'Check', 'Backup', 'Convert', 'Restore')]
    [string]$Mode,

    [string]$InstanceId = '',

    [Parameter(Mandatory = $true)]
    [string]$WorkDir,

    [Parameter(Mandatory = $true)]
    [string]$ResultPath,

    [string]$RunId = '',

    [string]$PlanPath = '',

    [string]$PythonPath = ''
)

$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + (Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)) 'WindowsPowerShell\Modules')
$ErrorActionPreference = 'Stop'
if (-not $PythonPath) {
    $PythonPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime\python\python.exe'
}

Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop

$script:ResultSchema = 'audio-codac.transmitter/1'
$script:BackupSchema = 'audio-codac.transmitter-backup/1'
$script:BluetoothClassGuid = 'e0cbf06c-cd8b-4647-bb8a-263b43f0f974'
$script:BluetoothCompatibleId = 'USB\Class_E0&SubClass_01&Prot_01'
# Locally pinned copy of raspberrypi/usbboot win32/redist/wdi-simple.exe. The parent copies it
# to tools/vendor/; a random wdi-simple from PATH is never used.
$script:HelperSha256 = 'E5C3C51396E5AEACC5B63D2991623894EB5A6A8981B7AEF680E1D0D8AF8A4387'
$script:HelperRelativePath = 'vendor\wdi-simple.exe'
$script:Libusb0HelperRelativePath = 'vendor\wdi-simple-libusb0.exe'
$script:Libusb0HelperSha256 = '199EFD67133FBD1B58F7698498F4FB3975E4A3C5EDA820347B1C72329D260277'
$script:Libusb0SysSha256 = 'CDA0070FE4F7C5DE5D3BB06FA6D7E29E6F6740C0F2FA319FB5C1AF235D78D616'

$WorkDir = [IO.Path]::GetFullPath($WorkDir)
$ResultPath = [IO.Path]::GetFullPath($ResultPath)
if (-not $RunId) { $RunId = [Guid]::NewGuid().ToString('N') }

function Write-JsonAtomic {
    param([string]$Path, [object]$Value)
    $directory = [IO.Path]::GetDirectoryName($Path)
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        [void](New-Item -ItemType Directory -Path $directory -Force)
    }
    $temporary = $Path + '.tmp.' + $PID + '.' + [Guid]::NewGuid().ToString('N')
    $json = ConvertTo-Json -InputObject $Value -Depth 12
    if ($null -eq $json) { $json = 'null' }
    [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-DeviceProperty {
    param([string]$Id, [string]$Key)
    try {
        return (Get-PnpDeviceProperty -InstanceId $Id -KeyName $Key -ErrorAction Stop).Data
    } catch {
        return $null
    }
}

function ConvertTo-ProblemCode {
    param($Value)
    if ($null -eq $Value) { return -1 }
    try { return [int]$Value } catch { return -1 }
}

function Get-UsbIdentity {
    param([string]$Id)
    if ($Id -notmatch '^USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})(?:&MI_([0-9A-Fa-f]{2}))?\\(.+)$') {
        return $null
    }
    $vid = [Convert]::ToInt32($matches[1], 16)
    $usbProductId = [Convert]::ToInt32($matches[2], 16)
    $interface = if ($matches[3]) { [Convert]::ToInt32($matches[3], 16) } else { -1 }
    $base = 'USB\VID_{0:X4}&PID_{1:X4}' -f $vid, $usbProductId
    $hardware = if ($interface -ge 0) { '{0}&MI_{1:X2}' -f $base, $interface } else { $base }
    [pscustomobject]@{
        Vid = $vid
        Pid = $usbProductId
        Interface = $interface
        Serial = [string]$matches[4]
        Base = $base
        HardwareId = $hardware
    }
}

function Test-HciIdentity {
    param([string]$Id)
    $classGuid = [string](Get-DeviceProperty $Id 'DEVPKEY_Device_ClassGuid')
    if ($classGuid) {
        $normalized = $classGuid.Trim('{', '}').ToLowerInvariant()
        if ($normalized -eq $script:BluetoothClassGuid) { return $true }
    }
    $compatible = @(Get-DeviceProperty $Id 'DEVPKEY_Device_CompatibleIds')
    foreach ($item in $compatible) {
        if ([string]$item -match '^USB\\Class_E0&SubClass_01&Prot_01$') { return $true }
    }
    return $false
}

function Get-TargetDevice {
    param([string]$Id)
    $found = @(Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object {
        [string]::Equals([string]$_.InstanceId, $Id, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($found.Count -ne 1) { throw 'target_missing' }
    return $found[0]
}

function Get-DeviceState {
    param([string]$Id)
    $device = Get-TargetDevice $Id
    [pscustomobject]@{
        InstanceId = [string]$device.InstanceId
        Status = [string]$device.Status
        Service = [string](Get-DeviceProperty $Id 'DEVPKEY_Device_Service')
        Inf = [string](Get-DeviceProperty $Id 'DEVPKEY_Device_DriverInfPath')
        Problem = ConvertTo-ProblemCode (Get-DeviceProperty $Id 'DEVPKEY_Device_ProblemCode')
    }
}

function Assert-UniqueVidPid {
    param([string]$Id, $Identity)
    $hits = @()
    $prefix = $Identity.HardwareId + '*'
    foreach ($device in @(Get-CimInstance Win32_PnPEntity -Filter "Present=TRUE AND PNPDeviceID LIKE 'USB%'")) {
        $deviceId = [string]$device.PNPDeviceID
        $hardwareIds = @($device.HardwareID)
        $match = $false
        foreach ($hardwareId in $hardwareIds) {
            if ([string]$hardwareId -like $prefix) { $match = $true; break }
        }
        if ($match) { $hits += $deviceId }
    }
    $hits = @($hits | Sort-Object -Unique)
    if ($hits.Count -ne 1 -or
        -not [string]::Equals($hits[0], $Id, [StringComparison]::OrdinalIgnoreCase)) {
        throw ('ambiguous_vid_pid:' + ($hits -join ';'))
    }
}

function Assert-NotCompositeParent {
    param([string]$Id)
    # Bluetooth enumerator children are normal; only a USB composite parent owns other USB functions.
    $service = [string](Get-DeviceProperty $Id 'DEVPKEY_Device_Service')
    $compatible = @(Get-DeviceProperty $Id 'DEVPKEY_Device_CompatibleIds')
    if ($service -ieq 'usbccgp' -or $compatible -contains 'USB\COMPOSITE') { throw 'composite_parent' }
}

function Get-InstanceHash {
    param([string]$Id)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Id.ToUpperInvariant())
        return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-BackupDir {
    param([string]$Id)
    return (Join-Path (Join-Path $WorkDir 'transmitters') (Get-InstanceHash $Id))
}

function Get-BackupInfo {
    param([string]$Id)
    $directory = Get-BackupDir $Id
    $metadataPath = Join-Path $directory 'backup.json'
    if (-not (Test-Path -LiteralPath $metadataPath)) { return $null }
    try {
        $metadata = Get-Content -Raw -LiteralPath $metadataPath | ConvertFrom-Json
    } catch {
        throw 'backup_invalid'
    }
    if ([string]$metadata.schema -ne $script:BackupSchema) { throw 'backup_invalid' }
    if (-not [string]::Equals([string]$metadata.instance_id, $Id,
            [StringComparison]::OrdinalIgnoreCase)) { throw 'backup_invalid' }
    $relative = [string]$metadata.original_inf_relative
    if (-not $relative) { throw 'backup_invalid' }
    $root = [IO.Path]::GetFullPath($directory)
    $infPath = [IO.Path]::GetFullPath((Join-Path $directory $relative))
    $rootPrefix = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $infPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'backup_invalid'
    }
    if (-not (Test-Path -LiteralPath $infPath)) { throw 'backup_invalid' }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $infPath).Hash
    if ($hash -ne [string]$metadata.original_inf_sha256) { throw 'backup_invalid' }

    $inbox = $false
    if ($metadata.PSObject.Properties.Name -contains 'original_inf_inbox') {
        $inbox = [bool]$metadata.original_inf_inbox
    } else {
        $inbox = ([string]$metadata.original_inf -notmatch '^oem[0-9]+\.inf$')
    }
    if ($inbox) {
        # The backup copy proves which inbox INF was active. Restore it through the Windows
        # INF directory so the original catalog/signature stays available, but only when the
        # current inbox INF is still byte-identical to the recorded backup.
        $windowsInf = Join-Path (Join-Path $env:WINDIR 'INF') ([string]$metadata.original_inf)
        if (-not (Test-Path -LiteralPath $windowsInf)) { throw 'backup_invalid' }
        $windowsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $windowsInf).Hash
        if ($windowsHash -ne [string]$metadata.original_inf_sha256) { throw 'backup_invalid' }
        $restoreInf = $windowsInf
    } else {
        $restoreInf = $infPath
    }
    [pscustomobject]@{ Dir = $directory; Metadata = $metadata; InfPath = $restoreInf }
}

function Test-BackupAvailable {
    param([string]$Id, $Identity = $null)
    try {
        $backup = Get-BackupInfo $Id
        if (-not $backup) { return $false }
        if ($Identity -and -not [string]::Equals([string]$backup.Metadata.hardware_id,
                [string]$Identity.HardwareId, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        return $true
    } catch { return $false }
}

function New-OriginalBackup {
    param([string]$Id, $Identity)
    $existing = Get-BackupInfo $Id
    if ($existing) {
        if (-not [string]::Equals([string]$existing.Metadata.hardware_id,
                [string]$Identity.HardwareId, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'backup_invalid'
        }
        return $existing
    }

    $directory = Get-BackupDir $Id
    [void](New-Item -ItemType Directory -Path $directory -Force)
    $infToken = [string](Get-DeviceProperty $Id 'DEVPKEY_Device_DriverInfPath')
    $service = [string](Get-DeviceProperty $Id 'DEVPKEY_Device_Service')
    if (-not $infToken -or -not $service) { throw 'missing_original_inf' }
    if ($infToken -ieq 'WinUSB') { throw 'already_winusb' }

    $relative = ''
    $inbox = $true
    if ($infToken -match '^oem[0-9]+\.inf$') {
        $inbox = $false
        # Export exactly the selected third-party oem INF package.
        $exportDirectory = Join-Path $directory ('driver-' + [Guid]::NewGuid().ToString('N'))
        [void](New-Item -ItemType Directory -Path $exportDirectory -Force)
        & pnputil.exe /export-driver $infToken $exportDirectory 2>&1 |
            Out-File -LiteralPath (Join-Path $directory 'export.log') -Encoding utf8
        if ($LASTEXITCODE -ne 0) { throw ('backup_export_failed:' + $LASTEXITCODE) }
        $infFiles = @(Get-ChildItem -LiteralPath $exportDirectory -Recurse -File -Filter *.inf)
        $named = @($infFiles | Where-Object { $_.Name -ieq $infToken })
        if ($named.Count -eq 1) {
            $chosen = $named[0]
        } elseif ($infFiles.Count -eq 1) {
            $chosen = $infFiles[0]
        } else {
            throw 'backup_export_failed:ambiguous'
        }
        $relative = $chosen.FullName.Substring($directory.Length + 1)
    } else {
        # Inbox INF: bth.inf is copied byte-for-byte from the Windows INF directory.
        $source = Join-Path (Join-Path $env:WINDIR 'INF') $infToken
        if (-not (Test-Path -LiteralPath $source)) { throw 'missing_original_inf' }
        $relative = 'original-' + [IO.Path]::GetFileNameWithoutExtension($infToken) + '.inf'
        Copy-Item -LiteralPath $source -Destination (Join-Path $directory $relative) -Force
    }

    $infPath = Join-Path $directory $relative
    $backup = [ordered]@{
        schema = $script:BackupSchema
        instance_id = $Id
        vid = $Identity.Vid
        pid = $Identity.Pid
        serial = $Identity.Serial
        interface = $Identity.Interface
        hardware_id = $Identity.HardwareId
        original_service = $service
        original_inf = $infToken
        original_inf_relative = $relative
        original_inf_inbox = $inbox
        original_inf_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $infPath).Hash
        original_problem = ConvertTo-ProblemCode (Get-DeviceProperty $Id 'DEVPKEY_Device_ProblemCode')
        created_at = [DateTimeOffset]::Now.ToString('o')
    }
    Write-JsonAtomic (Join-Path $directory 'backup.json') $backup
    return (Get-BackupInfo $Id)
}

function Invoke-Binding {
    param([string]$HardwareId, [string]$InfPath)
    if (-not ('AudioCodacTransmitterDriverBinding' -as [type])) {
        Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class AudioCodacTransmitterDriverBinding {
 [DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)]
 [return: MarshalAs(UnmanagedType.Bool)]
 public static extern bool UpdateDriverForPlugAndPlayDevices(
     IntPtr window, string hardwareId, string fullInf, uint flags,
     [MarshalAs(UnmanagedType.Bool)] out bool rebootRequired);
}
'@
    }
    $reboot = $false
    $ok = [AudioCodacTransmitterDriverBinding]::UpdateDriverForPlugAndPlayDevices(
        [IntPtr]::Zero, $HardwareId, $InfPath, 1, [ref]$reboot)
    if (-not $ok) {
        throw ('update_failed:' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    return $reboot
}

function Invoke-OriginalRollback {
    param([string]$Id, $Backup)
    $rollback = [ordered]@{
        attempted = $false
        success = $false
        service = $null
        problem = $null
        error = $null
    }
    if (-not $Backup) { return $rollback }
    $rollback.attempted = $true
    try {
        $reboot = Invoke-Binding ([string]$Backup.Metadata.hardware_id) $Backup.InfPath
        Start-Sleep -Seconds 2
        $state = Get-DeviceState $Id
        $rollback.service = $state.Service
        $rollback.problem = $state.Problem
        $rollback.success = (
            [string]::Equals($state.Service, [string]$Backup.Metadata.original_service,
                [StringComparison]::OrdinalIgnoreCase) -and
            $state.Problem -eq 0 -and -not $reboot)
        if (-not $rollback.success) { $rollback.error = 'rollback_verify_failed' }
    } catch {
        $rollback.error = $_.Exception.Message
    }
    return $rollback
}

function Get-UsbBluetoothRadios {
    $items = @()
    # One PnP snapshot avoids hundreds of individual property queries on a busy PC.
    foreach ($device in @(Get-CimInstance Win32_PnPEntity -Filter "Present=TRUE AND PNPDeviceID LIKE 'USB%'")) {
        $id = [string]$device.PNPDeviceID
        $identity = Get-UsbIdentity $id
        if (-not $identity) { continue }
        $classGuid = ([string]$device.ClassGuid).Trim('{','}').ToLowerInvariant()
        $compatible = @($device.CompatibleID)
        if ($classGuid -ne $script:BluetoothClassGuid -and
            $compatible -notcontains $script:BluetoothCompatibleId) { continue }
        $service = [string]$device.Service
        if ($service -ieq 'usbccgp' -or $compatible -contains 'USB\COMPOSITE') { continue }
        $name = [string]$device.Name
        if (-not $name) { $name = $id }
        $items += [ordered]@{
            instance_id = $id
            name = $name
            service = $service
            driver_inf = [string](Get-DeviceProperty $id 'DEVPKEY_Device_DriverInfPath')
            backend = $(if ($service -ieq 'WinUSB') { 'winusb' } elseif ($service -ieq 'libusb0') { 'libusb0' } else { '' })
            vid = $identity.Vid
            pid = $identity.Pid
            serial = $identity.Serial
            interface = $identity.Interface
            restore_available = (Test-BackupAvailable $id $identity)
            status = [string]$device.Status
            problem = ConvertTo-ProblemCode $device.ConfigManagerErrorCode
        }
    }
    return $items
}

function Assert-Administrator {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'admin_required'
    }
}

function Assert-GeneratedInfTarget {
    param([string]$Text, [string]$HardwareId, [string]$Backend = 'winusb')
    $expectedService = if ($Backend -eq 'winusb') { 'WinUSB' } elseif ($Backend -eq 'libusb0') { 'libusb0' } else { throw 'inf_service' }
    if ($Text -notmatch ('(?i)AddService\s*=\s*' + $expectedService + '\s*,')) { throw 'inf_service' }
    if ($Text -match '(?i)MS_COMP_WINUSB') { throw 'inf_wcid' }
    # libwdi's pinned template uses USB\%DeviceID% in each Models section;
    # validate the expanded target, not a nonexistent literal VID/PID line.
    $identities = [regex]::Matches($Text, '(?im)^\s*DeviceID\s*=\s*"(VID_[0-9A-F]{4}&PID_[0-9A-F]{4}(?:&MI_[0-9A-F]{2})?)"\s*$')
    if ($identities.Count -ne 1) { throw 'inf_not_specific' }
    $expanded = [regex]::Replace($Text, '(?i)%DeviceID%', $identities[0].Groups[1].Value)
    $section = if ($Backend -eq 'winusb') { 'USB_Install' } else { 'LIBUSB_WIN32_DEV(?:\.NT(?:AMD64|ARM64)?)?' }
    $targets = [regex]::Matches($expanded, '(?im)^\s*%DeviceName%\s*=\s*' + $section + '\s*,\s*(\S+)\s*$')
    $expectedCount = if ($Backend -eq 'winusb') { 3 } else { 4 }
    if ($targets.Count -ne $expectedCount) { throw 'inf_not_specific' }
    foreach ($target in $targets) {
        if (-not [string]::Equals($target.Groups[1].Value, $HardwareId,
                [StringComparison]::OrdinalIgnoreCase)) { throw 'inf_not_specific' }
    }
    # Reject a second literal hardware identity anywhere in the generated INF.
    foreach ($match in [regex]::Matches($expanded, '(?i)USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}(?:&MI_[0-9A-F]{2})?')) {
        if (-not [string]::Equals($match.Value, $HardwareId,
                [StringComparison]::OrdinalIgnoreCase)) { throw 'inf_not_specific' }
    }
}

function Invoke-ExecutorPython {
    param([string]$ScriptPath, [string]$Arguments)
    if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'executor_python_missing' }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $PythonPath
    $info.Arguments = '"' + $ScriptPath + '" ' + $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw 'executor_python_failed' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) { $process.Kill(); throw 'executor_python_timeout' }
        $output = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'executor_verification_failed' }
        return ($output | ConvertFrom-Json)
    } finally { $process.Dispose() }
}

function Invoke-DescriptorPreflight {
    param([string]$Id, $Identity)
    $root = Split-Path -Parent $PSScriptRoot
    # Cached descriptors only: no controller open/claim/reset or firmware/bond access.
    $report = Invoke-ExecutorPython (Join-Path $root 'inspect_adapter.py') ('--instance-id "' + $Id.ToUpperInvariant() + '" --with-identity')
    if ($report.schema -ne 'co-link.adapter-report/1' -or $report.matches -ne 1 -or
        $report.instance_id -cne $Id.ToUpperInvariant() -or
        $report.vid -ne $Identity.Vid -or $report.pid -ne $Identity.Pid) { throw 'preflight_invalid' }
    $stable = Invoke-ExecutorPython (Join-Path $root 'identity.py') (
        '--instance-id "' + $Id.ToUpperInvariant() + '" --vid 0x{0:X4} --pid 0x{1:X4}' -f $Identity.Vid, $Identity.Pid)
    if ($stable.schema -ne 'co-link.dongle-identity/1' -or
        $stable.vid -ne $report.vid -or $stable.pid -ne $report.pid) { throw 'stable_identity_unavailable' }
    $report | Add-Member -NotePropertyName dongle_identity -NotePropertyValue $stable -Force
    return $report
}

function Read-VerifiedPlan {
    param($Report)
    $root = Split-Path -Parent $PSScriptRoot
    $reportPath = Join-Path $WorkDir ('verify-' + [Guid]::NewGuid().ToString('N') + '.json')
    Write-JsonAtomic $reportPath $Report
    if(-not $PlanPath){
        # This optional private authority is absent from the public helper package.
        $localPolicy=Join-Path $root 'local_policy.py'
        if(!(Test-Path -LiteralPath $localPolicy -PathType Leaf)){throw 'signed_plan_required'}
        $plan=Invoke-ExecutorPython $localPolicy ('--report "'+$reportPath+'" --job-id "'+$RunId+'"')
    }else{
        if(!(Test-Path -LiteralPath $PlanPath -PathType Leaf)){throw 'signed_plan_required'}
        # The verification trust store is installed, never supplied by the job.
        $plan = Invoke-ExecutorPython (Join-Path $root 'plan.py') (
            '--plan "' + [IO.Path]::GetFullPath($PlanPath) + '" --report "' + $reportPath + '"')
    }
    if ($plan.operation -ne 'bind-transmitter' -or $plan.instance_id -cne $InstanceId.ToUpperInvariant() -or
        $plan.backend -notin @('winusb','libusb0')) { throw 'plan_device_mismatch' }
    return $plan
}

# ---------------------------------------------------------------------------------------------
# List is read-only: no administrator token, no mutation, no service or state-file write.
# ---------------------------------------------------------------------------------------------
if ($Mode -eq 'List') {
    try {
        $entries = @(Get-UsbBluetoothRadios)
        Write-JsonAtomic $ResultPath $entries
        $json = ConvertTo-Json -InputObject $entries -Depth 8
        [Console]::Out.Write($json)
        exit 0
    } catch {
        try { Write-JsonAtomic $ResultPath @() } catch { }
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}

# Read-only entry point for the future compatibility-before-payment flow.
# Descriptor success is a candidate; it is not a radio/link qualification.
if ($Mode -eq 'Check') {
    try {
        $identity = Get-UsbIdentity $InstanceId
        if (-not $identity -or -not (Test-HciIdentity $InstanceId)) { throw 'not_hci' }
        $null = Get-TargetDevice $InstanceId
        Assert-UniqueVidPid $InstanceId $identity
        Assert-NotCompositeParent $InstanceId
        $report = Invoke-DescriptorPreflight $InstanceId $identity
        Write-JsonAtomic $ResultPath $report
        [Console]::Out.Write((ConvertTo-Json $report -Depth 12))
        # Compatibility and payment eligibility are cloud-owned decisions.
        exit 0
    } catch {
        $failure = @{status='unavailable'; payment_eligible=$false; error=$_.Exception.Message}
        Write-JsonAtomic $ResultPath $failure
        [Console]::Out.Write((ConvertTo-Json $failure))
        exit 2
    }
}

$result = [ordered]@{
    schema = $script:ResultSchema
    mode = $Mode
    run_id = $RunId
    instance_id = $InstanceId
    success = $false
    usable = $false
    reboot_required = $false
    service = $null
    expected_service = $null
    backend = $null
    problem = $null
    status = $null
    backup = $null
    rollback = $null
    error = $null
    message = $null
}

try {
    . (Join-Path $PSScriptRoot 'trusted-path.ps1')
    Assert-TrustedAssistant -ScriptPath $PSCommandPath -PythonPath $PythonPath
    Assert-Administrator
    if (-not $InstanceId) { throw 'target_missing' }
    $identity = Get-UsbIdentity $InstanceId
    if (-not $identity) { throw 'not_hci' }
    if (-not (Test-HciIdentity $InstanceId)) { throw 'not_hci' }
    Assert-UniqueVidPid $InstanceId $identity
    Assert-NotCompositeParent $InstanceId
    $state = Get-DeviceState $InstanceId

    if ($Mode -eq 'Backup') {
        $backup = New-OriginalBackup $InstanceId $identity
        $result.backup = [ordered]@{
            path = $backup.Dir
            original_service = [string]$backup.Metadata.original_service
            original_inf = [string]$backup.Metadata.original_inf
            created = $true
        }
        $result.success = $true
        $result.usable = $false
        $result.message = 'original_backup_created'
        exit 0
    } elseif ($Mode -eq 'Convert') {
        $preflight = Invoke-DescriptorPreflight $InstanceId $identity
        $result.preflight = $preflight
        $plan = Read-VerifiedPlan $preflight
        $RunId = [string]$plan.job_id
        $result.run_id = $RunId
        $backend = [string]$plan.backend
        $expectedService = if ($backend -eq 'winusb') { 'WinUSB' } else { 'libusb0' }
        $result.backend = $backend
        $result.expected_service = $expectedService
        if ($state.Service -ieq $expectedService) {
            # Already converted: do not create or overwrite the original backup.
            $result.service = $state.Service
            $result.expected_service = $expectedService
            $result.problem = $state.Problem
            $result.status = $state.Status
            $result.usable = ($state.Problem -eq 0)
            $result.success = $result.usable
            if (-not $result.success) { $result.error = 'verify_failed' }
            $result.message = 'already_winusb'
            if (-not $result.success) { exit 1 }
            exit 0
        }

        if ($state.Service -in @('WinUSB','libusb0') -and -not (Test-BackupAvailable $InstanceId $identity)) {
            throw 'no_backup'
        }
        $backup = New-OriginalBackup $InstanceId $identity
        $result.backup = [ordered]@{
            path = $backup.Dir
            original_service = [string]$backup.Metadata.original_service
            original_inf = [string]$backup.Metadata.original_inf
            created = $false
        }

        $helperRelative = if ($backend -eq 'winusb') { $script:HelperRelativePath } else { $script:Libusb0HelperRelativePath }
        $helperHash = if ($backend -eq 'winusb') { $script:HelperSha256 } else { $script:Libusb0HelperSha256 }
        $helper = Join-Path $PSScriptRoot $helperRelative
        if (-not (Test-Path -LiteralPath $helper)) { throw 'helper_missing' }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $helper).Hash -ne $helperHash) {
            throw 'helper_hash'
        }

        # Keep earlier attempts for diagnosis/recovery rather than deleting their packages.
        $generated = Join-Path $backup.Dir ('generated-' + [Guid]::NewGuid().ToString('N'))
        [void](New-Item -ItemType Directory -Path $generated -Force)
        $infName = 'audio-codac-transmitter.inf'
        $wdiArguments = @(
            '-v', ('0x{0:X4}' -f $identity.Vid),
            '-p', ('0x{0:X4}' -f $identity.Pid),
            '-t', $(if ($backend -eq 'winusb') { '0' } else { '1' }),
            '-n', 'Audio co-DAC transmitter',
            '-m', 'Audio co-DAC',
            '-f', $infName,
            '-d', $generated,
            '-l', '0',
            '-o', '30000',
            '-x'
        )
        if ($identity.Interface -ge 0) {
            # wdi-simple -i <MI> makes DeviceID USB\VID_x&PID_y&MI_zz, never the composite parent.
            $wdiArguments += @('-i', [string]$identity.Interface)
        }
        # Windows PowerShell 5.1 turns native stderr (including libwdi debug logs)
        # into ErrorRecords. Those are not process failure; inspect its exit code.
        $savedErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $helper @wdiArguments 2>&1 |
                Out-File -LiteralPath (Join-Path $generated 'prepare.log') -Encoding utf8
            $prepareExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $savedErrorPreference
        }
        if ($prepareExit -ne 0) { throw ('prepare_failed:' + $prepareExit) }

        $infPath = Join-Path $generated $infName
        if (-not (Test-Path -LiteralPath $infPath)) { throw 'generated_inf_missing' }
        $infText = Get-Content -Raw -LiteralPath $infPath
        Assert-GeneratedInfTarget $infText $identity.HardwareId $backend
        if ($backend -eq 'libusb0') {
            $driverPath = Join-Path $generated 'amd64\libusb0.sys'
            if (-not (Test-Path -LiteralPath $driverPath) -or
                (Get-FileHash -Algorithm SHA256 -LiteralPath $driverPath).Hash -ne $script:Libusb0SysSha256) {
                throw 'driver_hash'
            }
            if ((Get-AuthenticodeSignature -LiteralPath $driverPath).Status.ToString() -ne 'Valid') {
                throw 'driver_signature'
            }
        }
        $catPath = Join-Path $generated 'audio-codac-transmitter.cat'
        if (-not (Test-Path -LiteralPath $catPath)) { throw 'generated_cat_missing' }
        $catStatus = (Get-AuthenticodeSignature -LiteralPath $catPath).Status.ToString()
        if ($catStatus -ne 'Valid') { throw ('cat_invalid:' + $catStatus) }

        # Verify the exact target again immediately before the binding mutation.
        $now = Get-DeviceState $InstanceId
        if ($now.Service -ieq $expectedService) {
            $result.service = $now.Service
            $result.expected_service = $expectedService
            $result.problem = $now.Problem
            $result.status = $now.Status
            $result.usable = ($now.Problem -eq 0)
            $result.success = $result.usable
            $result.message = 'already_winusb'
            if (-not $result.success) { exit 1 }
            exit 0
        }
        Assert-UniqueVidPid $InstanceId $identity
        Assert-NotCompositeParent $InstanceId
        # Recheck exact descriptors, target and expiry immediately before mutation.
        $null = Read-VerifiedPlan (Invoke-DescriptorPreflight $InstanceId $identity)

        $mutated = $false
        try {
            $mutated = $true
            $reboot = Invoke-Binding $identity.HardwareId $infPath
            Start-Sleep -Seconds 2
            $after = Get-DeviceState $InstanceId
            $result.service = $after.Service
            $result.expected_service = $expectedService
            $result.problem = $after.Problem
            $result.status = $after.Status
            $result.reboot_required = [bool]$reboot
            $usable = (
                [string]::Equals($after.Service, $expectedService,
                    [StringComparison]::OrdinalIgnoreCase) -and
                $after.Problem -eq 0 -and -not $reboot)
            $result.usable = $usable
            if ($reboot) {
                $result.error = 'reboot_pending'
                $result.message = 'The driver change is pending a restart and is not usable yet.'
            } elseif (-not $usable) {
                throw ('verify_failed:service=' + $after.Service + ';problem=' + $after.Problem)
            } else {
                $result.success = $true
            }
        } catch {
            if ($mutated) {
                $rollback = Invoke-OriginalRollback $InstanceId $backup
                $result.rollback = $rollback
                if ($rollback.service) { $result.service = $rollback.service }
                if ($null -ne $rollback.problem) { $result.problem = $rollback.problem }
            }
            throw
        }
    } else {
        $backup = Get-BackupInfo $InstanceId
        if (-not $backup) { throw 'no_backup' }
        if (-not [string]::Equals([string]$backup.Metadata.hardware_id,
                [string]$identity.HardwareId, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'backup_invalid'
        }
        $result.backup = [ordered]@{
            path = $backup.Dir
            original_service = [string]$backup.Metadata.original_service
            original_inf = [string]$backup.Metadata.original_inf
            created = $false
        }
        $expectedService = [string]$backup.Metadata.original_service
        $result.expected_service = $expectedService
        if ([string]::Equals($state.Service, $expectedService, [StringComparison]::OrdinalIgnoreCase) -and
            $state.Problem -eq 0) {
            $result.service = $state.Service
            $result.problem = $state.Problem
            $result.status = $state.Status
            $result.success = $true
            $result.usable = $true
            $result.message = 'already_original'
            exit 0
        }

        # Verify the exact target again immediately before the binding mutation.
        $null = Get-DeviceState $InstanceId
        Assert-UniqueVidPid $InstanceId $identity
        Assert-NotCompositeParent $InstanceId
        $reboot = Invoke-Binding ([string]$backup.Metadata.hardware_id) $backup.InfPath
        Start-Sleep -Seconds 2
        $after = Get-DeviceState $InstanceId
        $result.service = $after.Service
        $result.problem = $after.Problem
        $result.status = $after.Status
        $result.reboot_required = [bool]$reboot
        $usable = (
            [string]::Equals($after.Service, $expectedService,
                [StringComparison]::OrdinalIgnoreCase) -and
            $after.Problem -eq 0 -and -not $reboot)
        $result.usable = $usable
        if ($reboot) {
            $result.error = 'reboot_pending'
            $result.message = 'The driver change is pending a restart and is not usable yet.'
        } elseif (-not $usable) {
            throw ('restore_failed:service=' + $after.Service + ';problem=' + $after.Problem)
        } else {
            $result.success = $true
        }
    }
} catch {
    $message = $_.Exception.Message
    if ($message -match '^([a-z_]+)') { $result.error = $matches[1] } else { $result.error = 'failed' }
    $result.message = $message
    $result.success = $false
    $result.usable = $false
    if (-not $result.service -and $InstanceId) {
        try {
            $state = Get-DeviceState $InstanceId
            $result.service = $state.Service
            $result.problem = $state.Problem
            $result.status = $state.Status
        } catch { }
    }
    if (-not $result.expected_service) {
        if ($Mode -eq 'Convert') { $result.expected_service = 'WinUSB' }
    }
} finally {
    Write-JsonAtomic $ResultPath $result
}

if (-not $result.success) { exit 1 }
exit 0
