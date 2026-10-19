param(
    [Parameter(Mandatory=$true)][string]$ScriptPath,
    [Parameter(Mandatory=$true)][ValidateSet('Backup','Convert','Restore')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$WorkDir,
    [Parameter(Mandatory=$true)][string]$ResultPath,
    [Parameter(Mandatory=$true)][string]$InstanceId,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$PlanPath,
    [Parameter(Mandatory=$true)][string]$RunId
)
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + (Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)) 'WindowsPowerShell\Modules')
$ErrorActionPreference='Stop'

$trustCode=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'trusted-path.ps1') -Raw
$trustEncoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($trustCode))
$loadedTrust=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($trustEncoded))
. ([scriptblock]::Create($loadedTrust))
$expectedManager=Join-Path (Split-Path $PSCommandPath -Parent) 'manage-transmitter.ps1'
if ([IO.Path]::GetFullPath($ScriptPath) -ne [IO.Path]::GetFullPath($expectedManager)) { throw 'native_script_path_rejected' }
Assert-TrustedAssistant -ScriptPath $ScriptPath -PythonPath $PythonPath
foreach($p in @($WorkDir,$ResultPath,$PlanPath)) {
    if(-not [IO.Path]::IsPathRooted($p)) { throw 'absolute_path_required' }
}
$payload=[ordered]@{script=$ScriptPath;mode=$Mode;work=$WorkDir;result=$ResultPath;
    instance=$InstanceId;python=$PythonPath;plan=$PlanPath;run=$RunId;trust=$trustEncoded} | ConvertTo-Json -Compress
$payload64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
$child='$j=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('''+$payload64+'''))|ConvertFrom-Json;'
$child+='$t=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($j.trust));. ([scriptblock]::Create($t));'
$child+='Assert-TrustedAssistant -ScriptPath $j.script -PythonPath $j.python;'
$child+='& $j.script -Mode $j.mode -WorkDir $j.work -ResultPath $j.result -InstanceId $j.instance -PythonPath $j.python -PlanPath $j.plan -RunId $j.run'
$encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($child))
Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand',$encoded) -Verb RunAs -WindowStyle Hidden -Wait
