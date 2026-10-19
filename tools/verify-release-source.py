"""Offline byte comparison; no device/driver/server actions."""
from pathlib import Path
import hashlib,json,sys,zipfile
root=Path(__file__).resolve().parents[1]
m=json.loads((root/'SOURCE-MANIFEST.json').read_text())
if len(sys.argv)!=3:raise SystemExit('Usage: verify-release-source.py <setup.zip> <assistant.zip>')
archives={}
for arg in sys.argv[1:]:
    p=Path(arg);expected=m['archives'].get(p.name)
    if not expected or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:raise SystemExit('Archive hash mismatch: '+p.name)
    archives[p.name]=zipfile.ZipFile(p)
count=0
for item in m['files']:
    raw=(root/item['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise SystemExit('Source hash mismatch: '+item['path'])
    if 'member' in item:
        if raw!=archives[item['origin']].read(item['member']):raise SystemExit('Released source differs: '+item['path'])
        count+=1
print('PASS: '+str(count)+' source files match the exact released archive bytes; no device operations.')
