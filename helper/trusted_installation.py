"""Check protected installation files by SID and effective write-capable ACL entries."""
from __future__ import annotations
import base64, ctypes, json, os, subprocess
from pathlib import Path

TRUSTED_SIDS={"S-1-5-32-544","S-1-5-18","S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
# Do not OR FullControl/Modify: those composite masks include read permissions.
WRITE_MASK=0x2|0x4|0x10|0x40|0x100|0x10000|0x40000|0x80000|0x10000000|0x40000000

def approved_root(program_files=None):
    if program_files is None:
        if os.name!="nt":return Path("/unsupported")
        buf=ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None,0x26,None,0,buf)!=0:raise OSError("Program Files unavailable")
        program_files=buf.value
    return Path(os.path.abspath(Path(program_files)/"co-Link Conversion Assistant"))

def _has_reparse_ancestor(path):
    for part in (Path(path),*Path(path).parents):
        try:
            if part.is_symlink() or getattr(part.lstat(),"st_file_attributes",0)&0x400:return True
        except OSError:return True
    return False

def _protected_acls(paths,runner):
    code=r'''$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$paths=([Console]::In.ReadToEnd()|ConvertFrom-Json)
$rows=@(foreach($p in $paths){$a=if([IO.Directory]::Exists($p)){[IO.Directory]::GetAccessControl($p)}else{[IO.File]::GetAccessControl($p)}
$r=@(foreach($rule in $a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){if($rule.AccessControlType -eq 'Allow' -and ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0){
@{sid=$rule.IdentityReference.Value;rights=[int]$rule.FileSystemRights}}})
@{path=$p;owner=$a.GetOwner([Security.Principal.SecurityIdentifier]).Value;rules=$r}})
ConvertTo-Json -InputObject $rows -Depth 5 -Compress'''
    encoded=base64.b64encode(code.encode('utf-16le')).decode()
    try:
        result=runner(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
            input=json.dumps([str(p) for p in paths]),capture_output=True,text=True,encoding='utf-8',timeout=60,check=False)
        if result.returncode:return False
        rows=json.loads(result.stdout)
        if not isinstance(rows,list) or len(rows)!=len(paths):return False
        for row,path in zip(rows,paths):
            if row.get('path')!=str(path) or row.get('owner') not in TRUSTED_SIDS:return False
            for rule in row.get('rules',[]):
                if int(rule['rights'])&WRITE_MASK and rule['sid'] not in TRUSTED_SIDS:return False
        return True
    except (OSError,subprocess.SubprocessError,ValueError,KeyError,TypeError):return False

def check_trusted_installation(executor_dir,python_exe,*,program_files=None,runner=subprocess.run,platform_name=None):
    if (platform_name or os.name)!='nt':return False
    try:
        root=approved_root(program_files)
        executor=Path(os.path.abspath(executor_dir));python=Path(os.path.abspath(python_exe))
        if executor.parent!=root or python!=executor/'runtime/python/python.exe':return False
        expected=[executor/name for name in ('helper_server.py','helper_runtime.py','identity.py','inspect_adapter.py',
            'plan.py','plan-public-keys.json','trusted_installation.py','native/manage-transmitter.ps1','native/elevate-helper.ps1','native/trusted-path.ps1')]
        if not python.is_file() or not all(p.is_file() for p in expected):return False
        paths=[root.parent,root,executor,*executor.rglob('*')]
        if any(_has_reparse_ancestor(p) for p in paths):return False
        return _protected_acls(paths,runner)
    except OSError:return False
