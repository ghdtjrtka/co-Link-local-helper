function Assert-TrustedAssistant([string]$ScriptPath,[string]$PythonPath) {
    $programFiles=[Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    $root=[IO.Path]::GetFullPath((Join-Path $programFiles 'co-Link Conversion Assistant')).TrimEnd('\')
    $script=[IO.Path]::GetFullPath($ScriptPath);$python=[IO.Path]::GetFullPath($PythonPath)
    $executor=Split-Path (Split-Path $script -Parent) -Parent
    if($script -ne (Join-Path $executor 'native\manage-transmitter.ps1') -or
       $python -ne (Join-Path $executor 'runtime\python\python.exe') -or
       (Split-Path $executor -Parent).TrimEnd('\') -ne $root){throw 'trusted_installation_required'}
    $required=@('helper_server.py','helper_runtime.py','identity.py','inspect_adapter.py','plan.py',
        'trusted_installation.py','plan-public-keys.json','native\elevate-helper.ps1','native\trusted-path.ps1','native\manage-transmitter.ps1','runtime\python\python.exe')
    foreach($name in $required){if(!(Test-Path -LiteralPath (Join-Path $executor $name) -PathType Leaf)){throw 'trusted_file_missing'}}
    $trusted=@('S-1-5-32-544','S-1-5-18','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $writeMask=0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
    $paths=@($programFiles,$root,$executor)+@(Get-ChildItem -LiteralPath $executor -Recurse -Force | ForEach-Object {$_.FullName})
    foreach($path in $paths){
        $walk=Get-Item -LiteralPath $path -Force
        while($null -ne $walk){
            if(($walk.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'trusted_reparse_path'}
            $walk=if($walk.Parent){$walk.Parent}else{$null}
        }
        $acl=Get-Acl -LiteralPath $path
        if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted){throw 'trusted_owner_required'}
        foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
            if($rule.AccessControlType -ne 'Allow' -or ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0){continue}
            $sid=$rule.IdentityReference.Value
            if($sid -notin $trusted -and ([int]$rule.FileSystemRights -band $writeMask) -ne 0){throw 'trusted_acl_writable'}
        }
    }
}
