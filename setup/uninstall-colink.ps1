[CmdletBinding()]
param([switch]$Execute)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$receipt=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'installation.json') -Raw | ConvertFrom-Json
if($receipt.schema -ne 1 -or $receipt.edition -notin @('Standard','Personal','Steam')){throw 'Invalid installation receipt.'}
$root=[IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$base=[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA ('Programs\co-Link\'+$receipt.edition))).TrimEnd('\')
if($root -ne [IO.Path]::GetFullPath($receipt.install_root).TrimEnd('\') -or
   (Split-Path -Parent $root) -ne $base -or (Split-Path -Leaf $root) -notmatch '^[a-f0-9]{64}$'){throw 'Installation target is outside its product root.'}
$cursor=$root
while($cursor.Length -ge [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\').Length){
 if((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse point in uninstall target.'}
 $cursor=Split-Path -Parent $cursor
}
if(-not $Execute){Write-Output ('Preview: uninstall only '+$receipt.display_name+' at '+$root);return}
$folders=New-Object 'System.Collections.Generic.Stack[string]'
$folders.Push($root)
while($folders.Count -gt 0){
 foreach($entry in Get-ChildItem -LiteralPath $folders.Pop() -Force){
  if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Uninstall target contains a reparse point; no files removed.'}
  if($entry.PSIsContainer){$folders.Push($entry.FullName)}
 }
}
$exe=Join-Path $root 'co-Link.exe'
if(Get-Process -Name co-Link -ErrorAction SilentlyContinue | Where-Object {$_.Path -eq $exe}){throw 'Close this installed product before uninstalling.'}
$shortcut=Join-Path ([Environment]::GetFolderPath('StartMenu')) ('Programs\'+$receipt.display_name+'.lnk')
if(Test-Path -LiteralPath $shortcut){
 $shell=New-Object -ComObject WScript.Shell
 if($shell.CreateShortcut($shortcut).TargetPath -eq $exe){Remove-Item -LiteralPath $shortcut}
}
$key='HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\co-Link.'+$receipt.edition
if(Test-Path -LiteralPath $key){
 if((Get-ItemProperty -LiteralPath $key).InstallLocation -eq $root){Remove-Item -LiteralPath $key -Recurse}
}
# Absolute target and edition boundary were checked above. Other editions and user
# settings/bonds are intentionally outside this content-version directory.
Remove-Item -LiteralPath $root -Recurse -Force
