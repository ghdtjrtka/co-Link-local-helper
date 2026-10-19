param([string]$SourceDirectory = (Split-Path $PSCommandPath -Parent), [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version='0.1.1')
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + (Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)) 'WindowsPowerShell\Modules')
$ErrorActionPreference = 'Stop'
$Source = [IO.Path]::GetFullPath($SourceDirectory)
if (-not (Test-Path -LiteralPath (Join-Path $Source 'helper_server.py') -PathType Leaf)) { throw 'assistant_package_missing' }
$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $args = @('-NoProfile','-File',("`"$PSCommandPath`""),'-SourceDirectory',("`"$Source`""),'-Version',$Version)
    $elevated = Start-Process -FilePath 'powershell.exe' -ArgumentList $args -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    exit $elevated.ExitCode
}

$programFiles=[Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
$product = Join-Path $programFiles 'co-Link Conversion Assistant'
$destination = Join-Path $product $Version
if (Test-Path -LiteralPath $destination) { throw 'install_destination_already_exists' }
if (Test-Path -LiteralPath $product) {
    $productItem = Get-Item -LiteralPath $product -Force
    if (($productItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or -not $productItem.PSIsContainer) { throw 'install_product_path_invalid' }
    foreach ($rule in (Get-Acl -LiteralPath $product).GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $sid = $rule.IdentityReference.Value
        $writers = @('S-1-5-32-544','S-1-5-18')
        if ($sid -notin $writers -and ([int]$rule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::Write) -ne 0) {
            throw 'install_product_acl_writable'
        }
    }
}
$copyRoots = @('runtime','native','licenses','radio')
$copyFiles = @('helper_server.py','helper_runtime.py','transport_readiness.py','identity.py','inspect_adapter.py',
    'plan.py','plan-public-keys.json','trusted_installation.py','requirements.txt',
    'SOURCE-PROVENANCE.json','CONTENTS.sha256','Run Assistant.cmd','Start Assistant.cmd','Install Assistant.cmd','README.txt')
if(Test-Path -LiteralPath (Join-Path $Source 'co-link-local-converter.dll')){
    if(!(Test-Path -LiteralPath (Join-Path $Source 'local_policy.py'))){throw 'local_module_incomplete'}
    $copyFiles+=@('co-link-local-converter.dll','local_policy.py')
}elseif(Test-Path -LiteralPath (Join-Path $Source 'local_policy.py')){throw 'local_module_incomplete'}
$manifest = @{}
foreach ($line in (Get-Content -LiteralPath (Join-Path $Source 'CONTENTS.sha256'))) {
    if ($line -notmatch '^([a-fA-F0-9]{64})  (.+)$') { throw 'package_manifest_invalid' }
    $fileHash = $Matches[1].ToUpperInvariant()
    $relative = $Matches[2].Replace('/','\')
    if ([IO.Path]::IsPathRooted($relative) -or $relative.Split('\') -contains '..' -or $manifest.ContainsKey($relative)) { throw 'package_manifest_invalid' }
    $allowed = ($relative -match '^(runtime|native|licenses|radio)\\' -or $relative -in ($copyFiles + @('Install Assistant.cmd','Start Assistant.cmd')))
    if (-not $allowed) { throw 'package_file_not_allowlisted' }
    $manifest[$relative] = $fileHash
}
Get-ChildItem -LiteralPath $Source -Recurse -Force | ForEach-Object {
    if (($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'package_reparse_path' }
    if ($_.PSIsContainer) { return }
    $relative = $_.FullName.Substring($Source.Length).TrimStart('\')
    if ($relative -eq 'CONTENTS.sha256') { return }
    if (($manifest[$relative] -ne (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)) { throw 'package_file_hash_mismatch' }
}
foreach ($relative in ($copyRoots + $copyFiles)) {
    $item = Join-Path $Source $relative
    if (-not (Test-Path -LiteralPath $item)) { throw 'assistant_package_incomplete' }
    $check = Get-Item -LiteralPath $item -Force
    if (($check.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'package_reparse_path' }
}
New-Item -ItemType Directory -Path $product -Force | Out-Null
New-Item -ItemType Directory -Path $destination | Out-Null
try {
    foreach ($relative in $copyRoots) {
        Copy-Item -LiteralPath (Join-Path $Source $relative) -Destination $destination -Recurse
    }
    foreach ($relative in $copyFiles) {
        Copy-Item -LiteralPath (Join-Path $Source $relative) -Destination $destination
    }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]'S-1-5-32-544')
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $prop = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($entry in @(
        @('BUILTIN\Administrators',[Security.AccessControl.FileSystemRights]::FullControl),
        @('NT AUTHORITY\SYSTEM',[Security.AccessControl.FileSystemRights]::FullControl),
        @('BUILTIN\Users',[Security.AccessControl.FileSystemRights]::ReadAndExecute)
    )) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($entry[0],$entry[1],$inherit,$prop,$allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $product -AclObject $acl
    Set-Acl -LiteralPath $destination -AclObject $acl
    & icacls.exe $destination /setowner '*S-1-5-32-544' /T /Q | Out-Null
    if($LASTEXITCODE -ne 0){throw 'install_owner_failed'}
    foreach($relative in $manifest.Keys){
        $installedFile=Join-Path $destination $relative
        if(!(Test-Path -LiteralPath $installedFile -PathType Leaf) -or
           (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash -ne $manifest[$relative]){
            throw 'installed_package_hash_mismatch'
        }
    }
    $verify = Get-Acl -LiteralPath $destination
    $rules = @($verify.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
    foreach ($sid in @('S-1-5-32-544','S-1-5-18','S-1-5-32-545')) {
        $found = @($rules | Where-Object { $_.IdentityReference.Value -eq $sid })
        if (-not $found) { throw 'install_acl_verification_failed' }
        if ($sid -ne 'S-1-5-32-545' -and -not ($found | Where-Object {
            ([int]$_.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::FullControl) -eq
            [int][Security.AccessControl.FileSystemRights]::FullControl
        })) { throw 'install_acl_verification_failed' }
        foreach ($rule in $found) {
            if ($sid -eq 'S-1-5-32-545' -and $rule.AccessControlType -eq $allow) {
                $rights = [int]$rule.FileSystemRights
                $readExecute = [int][Security.AccessControl.FileSystemRights]::ReadAndExecute
                $write = [int][Security.AccessControl.FileSystemRights]::Write
                if (($rights -band $readExecute) -ne $readExecute -or ($rights -band $write) -ne 0) {
                    throw 'install_users_acl_invalid'
                }
            }
        }
    }
    Write-Output (Join-Path $destination 'Start Assistant.cmd')
} catch {
    # Destination was created by this run and never existed before it.
    $failed=Join-Path $product ($Version+'.failed-'+[Guid]::NewGuid().ToString('N'))
    if([IO.Path]::GetFullPath($destination).StartsWith([IO.Path]::GetFullPath($product)+'\',[StringComparison]::OrdinalIgnoreCase)){
        Move-Item -LiteralPath $destination -Destination $failed -ErrorAction SilentlyContinue
    }
    throw
}
