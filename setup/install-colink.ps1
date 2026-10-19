[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PackagePath,
    [switch]$Execute,
    [switch]$Interactive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProductNames = @{ Standard='co-Link Standard'; Personal='co-Link Developer'; Steam='co-Link Steam' }
if (-not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'This package requires Windows x64.' }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$UsbipRelease = [ordered]@{
    Tag = 'v.0.9.8.1'
    File = 'USBip-0.9.8.1-x64.exe'
    Url = 'https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.8.1/USBip-0.9.8.1-x64.exe'
    Bytes = 26083632L
    Sha256 = '38cad6d4432b52d5bb9409d9ad03b72fdffc4ada4cd3a48fbeca1a2752a8518a'
}

function Assert-SafeZipEntry([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name.Contains('\') -or $Name.StartsWith('/') -or $Name.Contains(':')) {
        throw "Unsafe package path: $Name"
    }
    $isDirectory = $Name.EndsWith('/')
    $parts = $Name.TrimEnd('/').Split('/')
    if (@($parts | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) {
        throw "Unsafe package path: $Name"
    }
    foreach ($part in $parts) {
        if ($part -match '[<>:"|?*]' -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
            throw "Windows-incompatible package path: $Name"
        }
        $baseName = ($part -split '\.', 2)[0]
        if ($baseName -match '^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$') {
            throw "Reserved Windows device name in package path: $Name"
        }
    }
    if ($isDirectory -and $Name -eq '/') { throw "Unsafe package path: $Name" }
}

function Get-ZipFileMap([System.IO.Compression.ZipArchive]$Archive) {
    $map = @{}
    $directories = @{}
    foreach ($entry in $Archive.Entries) {
        Assert-SafeZipEntry $entry.FullName
        $unixFileType = ($entry.ExternalAttributes -shr 16) -band 0xF000
        if (($entry.ExternalAttributes -band 0x400) -ne 0 -or $unixFileType -eq 0xA000) {
            throw "Reparse-point package entry is not allowed: $($entry.FullName)"
        }
        if ($entry.FullName.EndsWith('/')) {
            if ($directories.ContainsKey($entry.FullName)) { throw "Duplicate package directory: $($entry.FullName)" }
            $directories[$entry.FullName] = $true
            continue
        }
        if ($map.ContainsKey($entry.FullName)) { throw "Duplicate package entry: $($entry.FullName)" }
        $map[$entry.FullName] = $entry
    }
    foreach ($name in $map.Keys) {
        $segments = $name.Split('/')
        for ($i = 1; $i -lt $segments.Length; $i++) {
            $ancestor = ($segments[0..($i - 1)] -join '/')
            if ($map.ContainsKey($ancestor)) { throw "Package file is also used as a directory: $ancestor" }
        }
    }
    return @{ Files = $map; Directories = $directories }
}

function Get-ExpectedFiles([hashtable]$Entries) {
    if (-not $Entries.ContainsKey('CONTENTS.sha256')) { throw 'Package is missing CONTENTS.sha256.' }
    $reader = [IO.StreamReader]::new($Entries['CONTENTS.sha256'].Open())
    try { $manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
    $expected = @{}
    foreach ($line in ($manifest -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})  (.+)$') { throw "Malformed CONTENTS.sha256 line: $line" }
        $hash = $Matches[1].ToLowerInvariant()
        $name = $Matches[2]
        Assert-SafeZipEntry $name
        if (-not $Entries.ContainsKey($name)) { throw "Manifest file missing from package: $name" }
        if ($expected.ContainsKey($name)) { throw "Duplicate manifest entry: $name" }
        $stream = $Entries[$name].Open()
        try {
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = $sha.ComputeHash($stream) } finally { $sha.Dispose() }
            $actual = ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
        } finally { $stream.Dispose() }
        if ($actual -ne $hash) { throw "Package hash mismatch: $name" }
        $expected[$name] = $hash
    }
    if (-not $expected.ContainsKey('co-Link.exe')) { throw 'Package manifest does not contain co-Link.exe.' }
    foreach ($name in $Entries.Keys) {
        if ($name -ne 'CONTENTS.sha256' -and -not $expected.ContainsKey($name)) { throw "Unlisted package file: $name" }
    }
    return $expected
}

function Get-PackageVersion([hashtable]$Expected) {
    $canonical = [Text.StringBuilder]::new()
    foreach ($name in @($Expected.Keys | Sort-Object -CaseSensitive)) {
        [void]$canonical.Append($name).Append("`n").Append($Expected[$name]).Append("`n")
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes($canonical.ToString())
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Assert-NoReparsePath([string]$Path, [string]$StopAt) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $stop = [IO.Path]::GetFullPath($StopAt).TrimEnd('\')
    if (-not $fullPath.StartsWith($stop + '\', [StringComparison]::OrdinalIgnoreCase) -and $fullPath -ne $stop) {
        throw "Path is outside the allowed installation root: $fullPath"
    }
    $relative = $fullPath.Substring($stop.Length).TrimStart('\')
    $cursor = $stop
    if (Test-Path -LiteralPath $cursor) {
        if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point in installation path: $cursor" }
    }
    foreach ($part in ($relative -split '\\' | Where-Object { $_ })) {
        $cursor = Join-Path $cursor $part
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point in installation path: $cursor" }
        }
    }
}

function Show-InstallDialog([string]$InstallPath) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object Windows.Forms.Form
    $form.Text = 'Install ' + $productName
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.ClientSize = New-Object Drawing.Size(550, 230)
    $label = New-Object Windows.Forms.Label
    $label.AutoSize = $false
    $label.Location = New-Object Drawing.Point(16, 14)
    $label.Size = New-Object Drawing.Size(518, 100)
    $label.Text = "Install $productName for this Windows user at:`r`n$InstallPath`r`nNo device or system configuration is changed by app installation."
    $checkbox = New-Object Windows.Forms.CheckBox
    $checkbox.AutoSize = $false
    $checkbox.Location = New-Object Drawing.Point(18, 120)
    $checkbox.Size = New-Object Drawing.Size(518, 42)
    $checkbox.Text = 'Also install the optional DualSense USB/IP driver (upstream installer UI will appear)'
    $ok = New-Object Windows.Forms.Button
    $ok.Text = 'Install'
    $ok.Location = New-Object Drawing.Point(346, 182)
    $ok.DialogResult = [Windows.Forms.DialogResult]::OK
    $cancel = New-Object Windows.Forms.Button
    $cancel.Text = 'Cancel'
    $cancel.Location = New-Object Drawing.Point(438, 182)
    $cancel.DialogResult = [Windows.Forms.DialogResult]::Cancel
    $form.Controls.AddRange(@($label, $checkbox, $ok, $cancel))
    $form.AcceptButton = $ok
    $form.CancelButton = $cancel
    $result = $form.ShowDialog()
    $includeDriver = $checkbox.Checked
    $form.Dispose()
    if ($result -ne [Windows.Forms.DialogResult]::OK) { return $null }
    return [pscustomobject]@{ Install = $true; Usbip = $includeDriver }
}

function Find-UsbipExecutable {
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $root) { continue }
        $candidate = Join-Path $root "USBip\usbip.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Get-HealthyUsbipDevices {
    # Hardware ID from the upstream INF; Windows assigns an unrelated ROOT\USB\NNNN instance.
    @(Get-CimInstance Win32_PnPEntity -Filter "Service = 'usbip2_ude'" -ErrorAction Stop |
        Where-Object { $_.Present -and $_.ConfigManagerErrorCode -eq 0 -and
                       @($_.HardwareID) -contains 'ROOT\USBIP_WIN2\UDE' })
}

if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) { throw "Package ZIP not found: $PackagePath" }
$package = (Resolve-Path -LiteralPath $PackagePath).Path
$archive = [IO.Compression.ZipFile]::OpenRead($package)
try {
    $zip = Get-ZipFileMap $archive
    $entries = $zip.Files
    $expectedFiles = Get-ExpectedFiles $entries
    if (-not $expectedFiles.ContainsKey('product-edition.json')) { throw 'Package is missing product-edition.json; use an edition-aware package.' }
    $reader = [IO.StreamReader]::new($entries['product-edition.json'].Open())
    try { $identity = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
    if ($identity.schema -ne 1 -or -not $ProductNames.ContainsKey([string]$identity.edition)) { throw 'Invalid product edition.' }
    $edition = [string]$identity.edition
    $productName = $ProductNames[$edition]
    if ($edition -eq 'Standard') {
        foreach ($name in $expectedFiles.Keys) {
            if ($name -match '(^|/)(conversion-local\.json|co-link-local-converter\.dll|local_policy\.py|plan-signing[^/]*|bonds)(/|$)' -or $name -match '\.dpapi$') {
                throw "Local conversion/private material is forbidden in Standard: $name"
            }
        }
    }
} finally { $archive.Dispose() }

$installBase = Join-Path $env:LOCALAPPDATA ('Programs\co-Link\' + $edition)
$version = Get-PackageVersion $expectedFiles
$installRoot = Join-Path $installBase $version
$doInstall = [bool]$Execute
$installUsbip = $false
if ($Interactive) {
    $choice = Show-InstallDialog $installRoot
    if (-not $choice) { Write-Host 'Cancelled; no changes were made.'; return }
    if (-not $Execute) { Write-Host 'Confirmed preview only; no changes were made.'; return }
    $installUsbip = $choice.Usbip
}

Write-Host "Plan: validate $($expectedFiles.Count) manifest-listed files in $package"
Write-Host "Plan: install $productName for current user at $installRoot"
Write-Host 'Plan: no virtual audio cable, Steam, service, account, or unrelated device configuration changes'
if ($installUsbip) {
    Write-Host "Plan: optional DualSense USB/IP driver $($UsbipRelease.Tag), $($UsbipRelease.Bytes) bytes, SHA-256 $($UsbipRelease.Sha256)"
    Write-Host 'Plan: upstream installer will be shown interactively and may request a reboot'
}
if (-not $doInstall) { return }

# Install into a content-addressed version directory. Never overwrite or delete an existing version.
$existingVersion = Test-Path -LiteralPath $installRoot
Assert-NoReparsePath $installRoot $env:LOCALAPPDATA
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
Assert-NoReparsePath $installRoot $env:LOCALAPPDATA
$archive = [IO.Compression.ZipFile]::OpenRead($package)
try {
    $zip = Get-ZipFileMap $archive
    $entries = $zip.Files
    $actualExpected = Get-ExpectedFiles $entries
    if ((Get-PackageVersion $actualExpected) -ne $version) { throw 'Package contents changed after validation.' }
    foreach ($name in $expectedFiles.Keys) {
        $destination = [IO.Path]::GetFullPath((Join-Path $installRoot ($name -replace '/', '\')))
        Assert-NoReparsePath $destination $installRoot
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        Assert-NoReparsePath $destination $installRoot
        if ($existingVersion) {
            if (-not (Test-Path -LiteralPath $destination -PathType Leaf) -or
                (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedFiles[$name]) {
                throw "Existing version is incomplete or modified; preserved without overwrite: $destination"
            }
            continue
        }
        $inStream = $entries[$name].Open()
        try {
            $output = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $inStream.CopyTo($output) } finally { $output.Dispose() }
        } finally { $inStream.Dispose() }
        $extractedHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($extractedHash -ne $expectedFiles[$name]) { throw "Extracted file hash mismatch: $name" }
    }
} finally { $archive.Dispose() }

$probePath = Join-Path $installRoot 'installation-info.json'
$probeProcess = Start-Process -FilePath (Join-Path $installRoot 'co-Link.exe') -ArgumentList @('--installation-info',('"'+$probePath+'"')) -WindowStyle Hidden -PassThru
if (-not $probeProcess.WaitForExit(15000)) { throw 'Installed executable did not return installation identity; no shortcut or registration was created.' }
if ($probeProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $probePath)) { throw 'Installed executable identity check failed.' }
$actualIdentity = Get-Content -LiteralPath $probePath -Raw | ConvertFrom-Json
if ($actualIdentity.edition -ne $edition) { throw 'Executable edition does not match the package.' }
if ($edition -eq 'Standard' -and $actualIdentity.local_conversion_config) { throw 'Standard installation contains a local conversion configuration.' }
$shortcutPath = Join-Path ([Environment]::GetFolderPath('StartMenu')) ('Programs\' + $productName + '.lnk')
$shortcutDirectory = Split-Path -Parent $shortcutPath
New-Item -ItemType Directory -Path $shortcutDirectory -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $installRoot 'co-Link.exe'
$shortcut.WorkingDirectory = $installRoot
$shortcut.Save()
$uninstaller = Join-Path $PSScriptRoot 'uninstall-colink.ps1'
if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) { throw 'Setup is missing uninstall-colink.ps1.' }
Copy-Item -LiteralPath $uninstaller -Destination (Join-Path $installRoot 'uninstall-colink.ps1') -Force
$registration = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\co-Link.' + $edition
New-Item -Path $registration -Force | Out-Null
$receipt = [ordered]@{schema=1;edition=$edition;display_name=$productName;install_root=$installRoot;shortcut=$shortcutPath;package_sha256=(Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant();content_version=$version;driver_requested=$installUsbip}
[IO.File]::WriteAllText((Join-Path $installRoot 'installation.json'),($receipt | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
New-ItemProperty -Path $registration -Name DisplayName -Value $productName -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registration -Name DisplayVersion -Value $version.Substring(0,12) -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registration -Name InstallLocation -Value $installRoot -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registration -Name DisplayIcon -Value (Join-Path $installRoot 'co-Link.exe') -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registration -Name UninstallString -Value ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $installRoot 'uninstall-colink.ps1') + '" -Execute') -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registration -Name NoModify -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $registration -Name NoRepair -Value 1 -PropertyType DWord -Force | Out-Null
Write-Host "$productName installed for current user: $installRoot"

if ($installUsbip) {
    $healthy = @(Get-HealthyUsbipDevices)
    if ((Find-UsbipExecutable) -and $healthy.Count -gt 0) {
        Write-Host 'DualSense USB/IP is already installed and its device is healthy; no reinstall.'
        return
    }
    $installerPath = Join-Path $PSScriptRoot $UsbipRelease.File
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        $cacheRoot = Join-Path $env:TEMP ('co-Link-setup-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $cacheRoot | Out-Null
        $installerPath = Join-Path $cacheRoot $UsbipRelease.File
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $UsbipRelease.Url -OutFile $installerPath -UseBasicParsing
    }
    $file = Get-Item -LiteralPath $installerPath
    if ($file.Length -ne $UsbipRelease.Bytes) { throw "USB/IP installer size mismatch: $($file.Length)" }
    $actualHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $UsbipRelease.Sha256) { throw "USB/IP installer SHA-256 mismatch: $actualHash" }
    $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate) { throw "USB/IP installer Authenticode signature is not valid: $($signature.Status)" }
    $process = Start-Process -FilePath $installerPath -ArgumentList @('/NORESTART', '/RESTARTEXITCODE=3010') -Verb RunAs -Wait -PassThru
    if ($process.ExitCode -eq 4) { throw 'USB/IP installer returned fatal exit code 4.' }
    if ($process.ExitCode -eq 3010) { throw 'USB/IP installer requires a reboot (exit code 3010); installation is not reported complete. Reboot and rerun verification.' }
    if ($process.ExitCode -ne 0) { throw "USB/IP installer failed with exit code $($process.ExitCode)." }
    $usbipExe = Find-UsbipExecutable
    if (-not $usbipExe) { throw 'USB/IP installer exited successfully, but usbip.exe was not found. Installation is not reported complete.' }
    $devices = @(Get-HealthyUsbipDevices)
    if ($devices.Count -eq 0) { throw 'USB/IP installer exited successfully, but no healthy ROOT USBIP_WIN2 device was found. Installation is not reported complete.' }
    Write-Host "DualSense USB/IP driver verified: $usbipExe; $($devices.Count) healthy ROOT USBIP_WIN2 device(s)."
}
