"""Windows runtime bindings for the loopback helper; external effects stay behind methods."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

try:
    from . import identity
    from . import plan as plan_module
    from .inspect_adapter import inspect as inspect_descriptors
except ImportError:  # direct `python executor/helper_server.py` launch
    import identity
    import plan as plan_module
    from inspect_adapter import inspect as inspect_descriptors


class WindowsHelperRuntime:
    def __init__(self, state_dir, *, enable_conversion=False, runner=subprocess.run,
                 identity_reader=identity.inspect_identity, descriptor_reader=inspect_descriptors,
                 plan_verifier=plan_module.verify, clock=time.time, installation_checker=None, readiness_probe=None):
        self.root = Path(state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.enable_conversion = enable_conversion
        self.runner = runner
        self.identity_reader = identity_reader
        self.descriptor_reader = descriptor_reader
        self.plan_verifier = plan_verifier
        self.clock = clock
        try:
            from .transport_readiness import probe_transport
        except ImportError:
            from transport_readiness import probe_transport
        self.readiness_probe = readiness_probe or probe_transport
        try:
            from .trusted_installation import check_trusted_installation
        except ImportError:
            from trusted_installation import check_trusted_installation
        self.installation_checker = installation_checker or check_trusted_installation
        self.executor = Path(__file__).resolve().parent
        self.native = self.executor / "native"
        self.work = (self.root / "native-state").resolve()
        self.work.mkdir(parents=True, exist_ok=True)

    def _powershell(self, args, timeout=20):
        if os.name != "nt":
            raise RuntimeError("windows_runtime_required")
        return self.runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned", *map(str,args)],
                           capture_output=True, text=True, timeout=timeout, check=False)

    def health(self):
        issues=[]
        if os.name != "nt": issues.append("windows_runtime_required")
        if not (self.native/"manage-transmitter.ps1").is_file(): issues.append("native_helper_missing")
        candidates=[self.native/"inspect-radio.exe",self.native/"Release"/"inspect-radio.exe",
                    self.native/"build"/"Release"/"inspect-radio.exe"]
        if not any(p.is_file() for p in candidates): issues.append("native_radio_inspector_missing")
        return {"status":"ready" if not issues else "unavailable","errors":issues,"plan_schemas":["co-link.conversion-plan/3","co-link.recovery-plan/1"],"conversion_scopes":["dongle-lifetime","steam-software"]}

    def list_devices(self):
        manager = self.native / "manage-transmitter.ps1"
        result_path = self.work / ("list-" + uuid.uuid4().hex + ".json")
        completed = self._powershell(["-File", manager, "-Mode", "List", "-WorkDir", self.work,
                                      "-ResultPath", result_path], timeout=30)
        try:
            if not result_path.is_file(): raise RuntimeError("device_inventory_unavailable")
            devices = json.loads(result_path.read_text(encoding="utf-8-sig"))
            if not isinstance(devices, list): raise RuntimeError("device_inventory_invalid")
            return devices
        finally:
            result_path.unlink(missing_ok=True)

    def inspect(self, instance_id=None, *, prepare_backup=True):
        devices = self.list_devices()
        eligible = [d for d in devices if isinstance(d,dict) and isinstance(d.get("instance_id"),str)]
        if instance_id is None:
            if len(eligible) == 1:
                instance_id = eligible[0]["instance_id"]
            elif len(eligible) > 1:
                return {"devices":[{k:d.get(k) for k in ("instance_id","name","vid","pid","backend","status","problem")} for d in eligible]}
            else:
                raise RuntimeError("bluetooth_radio_not_found")
        if not isinstance(instance_id,str) or len(instance_id)>256:
            raise ValueError("invalid_instance_id")
        matched=[d for d in eligible if d.get("instance_id","\0").casefold()==instance_id.casefold()]
        if len(matched)!=1: raise ValueError("selected_radio_missing_or_ambiguous")
        selected=matched[0]
        report=self.descriptor_reader(instance_id.upper())
        identity_value=self.identity_reader(instance_id, int(report["vid"]), int(report["pid"]))
        report["dongle_identity"]=identity_value
        backup=self._ensure_backup(selected, instance_id) if prepare_backup else None
        return {"report":report,"identity":identity_value,"backup":backup}

    def inspect_for_execution(self,instance_id):
        # An unverified incoming plan can request public facts, never backup elevation.
        return self.inspect(instance_id,prepare_backup=False)

    def _ensure_backup(self, selected, instance_id):
        existing=self._backup_for(instance_id)
        if existing: return existing
        if selected.get("service","").casefold() in ("winusb","libusb0"):
            return {"id":None,"sha256":None,"downloaded":False,"error":"no_backup"}
        if not self.enable_conversion:
            return {"id":None,"sha256":None,"downloaded":False,"error":"backup_requires_enable_conversion"}
        self._run_elevated("Backup", instance_id, "backup-"+uuid.uuid4().hex, None)
        created=self._backup_for(instance_id)
        if not created: raise RuntimeError("backup_export_failed")
        return created

    def _backup_for(self, instance_id):
        key=hashlib.sha256(instance_id.upper().encode()).hexdigest()
        original=(self.work/"transmitters"/key).resolve()
        try: original.relative_to(self.work)
        except ValueError: raise RuntimeError("backup_path_invalid")
        metadata_path=original/"backup.json"
        if not metadata_path.is_file(): return None
        metadata=json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        if metadata.get("schema")!="audio-codac.transmitter-backup/1" or metadata.get("instance_id","\0").casefold()!=instance_id.casefold():
            raise RuntimeError("backup_metadata_invalid")
        inf=(original/str(metadata.get('original_inf_relative',''))).resolve()
        if not inf.is_relative_to(original) or not inf.is_file() or hashlib.sha256(inf.read_bytes()).hexdigest().lower()!=str(metadata.get('original_inf_sha256','')).lower():
            raise RuntimeError('backup_original_hash_mismatch')
        backup_id=hashlib.sha256((key+str(metadata.get("original_inf_sha256"))).encode()).hexdigest()[:32]
        outdir=self.root/"backups"; outdir.mkdir(parents=True,exist_ok=True)
        archive=outdir/(backup_id+".zip")
        if not archive.is_file():
            temporary=outdir/(backup_id+'.'+uuid.uuid4().hex+'.tmp')
            try:
                with zipfile.ZipFile(temporary,"w",zipfile.ZIP_DEFLATED) as zf:
                    for path in original.rglob("*"):
                        if path.is_symlink() or not path.resolve().is_relative_to(original):
                            raise RuntimeError('backup_path_invalid')
                        if path.is_file() and not path.name.endswith('.dpapi'):
                            zf.write(path,path.relative_to(original).as_posix())
                os.replace(temporary,archive)
            finally:
                temporary.unlink(missing_ok=True)
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        return {"id":backup_id,"sha256":digest,"downloaded":False,
                "original_service":metadata.get('original_service'),"original_inf":metadata.get('original_inf')}

    def backup_path(self, backup_id):
        if not re.fullmatch(r"[a-f0-9]{32}",backup_id): raise ValueError("invalid_backup_id")
        path=(self.root/"backups"/(backup_id+".zip")).resolve()
        path.relative_to((self.root/"backups").resolve())
        if not path.is_file(): raise FileNotFoundError("no_backup")
        return path

    def verify(self, envelope, report):
        keys=json.loads((self.executor/"plan-public-keys.json").read_text(encoding="utf-8-sig"))
        return self.plan_verifier(envelope, keys, report)

    def execute(self, envelope, report, _state):
        if not self.enable_conversion: raise PermissionError("conversion_disabled")
        plan=self.verify(envelope,report)
        if plan["operation"]!="bind-transmitter" or plan["backend"] not in ("winusb","libusb0"):
            raise ValueError("unsupported_plan")
        backup=self._backup_for(plan['instance_id'])
        if not backup or backup.get('id')!=plan.get('backup_id') or backup.get('sha256')!=plan.get('backup_sha256'):
            raise ValueError('execution_backup_mismatch')
        receipt=self.root/"jobs"/(plan["job_id"]+".json")
        receipt.parent.mkdir(parents=True,exist_ok=True)
        record=receipt.with_suffix('.target.json')
        if record.is_file():
            # A repeated request rechecks reality; it never silently binds a second time.
            saved=json.loads(record.read_text(encoding='utf8'))
            if plan_module.fingerprint(saved['report']) != plan_module.fingerprint(report):
                raise ValueError('job_target_changed')
            return self._verify_current(plan,report)
        record.write_text(json.dumps({'report':report,'backup':backup,'plan':plan}),encoding='utf8')
        result=self._run_elevated("Convert",plan["instance_id"],plan["job_id"],envelope)
        result=self._verify_current(plan,report,result)
        receipt.write_text(json.dumps(result,separators=(",",":")),encoding="utf-8")
        return result

    def _load_job(self,envelope,job_id,operation):
        if not self.enable_conversion: raise PermissionError('conversion_disabled')
        if not re.fullmatch(r'[a-f0-9]{32}',job_id): raise ValueError('invalid_job_id')
        path=self.root/'jobs'/(job_id+'.target.json')
        if not path.is_file():
            if operation!='restore-original': raise RuntimeError('local_job_record_missing')
            # If execution never reached this helper, the original driver is still inspectable.
            # Signature/identity verification below precedes any elevation or restore.
            import base64
            hint=json.loads(base64.b64decode(envelope['payload_b64'],validate=True)).get('instance_id')
            inspected=self.inspect_for_execution(hint)
            record={'report':inspected['report']}
        else:
            record=json.loads(path.read_text(encoding='utf8'))
        plan=self.verify(envelope,record['report'])
        if plan['job_id']!=job_id or plan['operation']!=operation: raise ValueError('job_operation_mismatch')
        return plan,record

    def verify_conversion(self,envelope,job_id):
        plan,record=self._load_job(envelope,job_id,'verify-transmitter')
        return self._verify_current(plan,record['report'])

    def _current_device(self,instance_id):
        matches=[d for d in self.list_devices() if d.get('instance_id','').casefold()==instance_id.casefold()]
        if len(matches)!=1: raise RuntimeError('selected_radio_missing_or_ambiguous')
        return matches[0]

    def _verify_current(self,plan,report,native=None):
        # Pending reboot must not be turned into success by a service name alone.
        result={'schema':'co-link.conversion-result/1','job_id':plan['job_id'],
            'instance_id':plan['instance_id'],'dongle_fingerprint':plan['dongle_fingerprint'],
            'execution_fingerprint':plan['fingerprint'],'success':False,'usable':False,
            'device_untouched':(native or {}).get('device_untouched') is True,
            'reboot_required':bool((native or {}).get('reboot_required')), 'service':'','problem':-1}
        try:
            current=self._current_device(plan['instance_id'])
            result.update(service=current.get('service',''),problem=current.get('problem',-1))
            result['success']=not result['reboot_required']
            if not self._usable(result,plan['backend']):
                result['success']=False
                return result
            fresh=self.descriptor_reader(plan['instance_id'])
            if plan_module.fingerprint(fresh)!=plan['fingerprint']: raise ValueError('post_bind_descriptors_changed')
            t=self.readiness_probe(report,plan['backend'],self.work,plan['job_id'])
            result['transport']=t
            complete=(t.get('status')=='complete' and t.get('closed') is True and
                t.get('controller_address')==report['dongle_identity']['controller_address'] and
                type(t.get('command_responses')) is int and t['command_responses']>=3 and
                type(t.get('acl_packet_length')) is int and t['acl_packet_length']>0 and
                type(t.get('total_acl_packets')) is int and t['total_acl_packets']>0)
            result['success']=result['usable']=complete
        except Exception:
            result['success']=result['usable']=False
            result['error']='transport_verification_failed'
        return result

    def restore(self,envelope,job_id):
        plan,record=self._load_job(envelope,job_id,'restore-original')
        backup=self._backup_for(plan['instance_id'])
        if (not backup or any(backup.get(k)!=plan.get(k if k!='sha256' else 'backup_sha256')
                for k in ('sha256','original_service','original_inf')) or backup['id']!=plan['backup_id']):
            raise ValueError('recovery_backup_mismatch')
        # Job identity is preserved locally even when a failed binding hides the OS radio address.
        current=self._current_device(plan['instance_id'])
        fresh=self.descriptor_reader(plan['instance_id'])
        if plan_module.fingerprint(fresh)!=plan_module.fingerprint(record['report']):
            raise ValueError('recovery_target_changed')
        native=self._run_elevated('Restore',plan['instance_id'],job_id+'-restore',envelope)
        current=self._current_device(plan['instance_id'])
        result={'schema':'co-link.recovery-result/1','job_id':job_id,'instance_id':plan['instance_id'],
                'dongle_fingerprint':plan['dongle_fingerprint'],'backup_sha256':backup['sha256'],
                'service':current.get('service',''),'driver_inf':current.get('driver_inf',''),
                'problem':current.get('problem',-1),'reboot_required':native.get('reboot_required') is True}
        result['success']=(native.get('success') is True and not result['reboot_required'] and result['problem']==0 and
            result['service'].casefold()==plan['original_service'].casefold() and
            result['driver_inf'].casefold()==plan['original_inf'].casefold())
        if native.get('error') == 'restore_unavailable' and not result['success']:
            result['error'] = 'restore_unavailable'
        return result

    @staticmethod
    def _usable(result,backend):
        expected='WinUSB' if backend=='winusb' else 'libusb0'
        return (result.get('success') is True and result.get('reboot_required') is False and
                type(result.get('problem')) is int and result['problem']==0 and
                str(result.get('service','')).casefold()==expected.casefold())

    def _run_elevated(self, mode, instance_id, run_id, envelope):
        launcher=self.native/"elevate-helper.ps1"; manager=self.native/"manage-transmitter.ps1"
        if not self.installation_checker(self.executor, sys.executable):
            raise PermissionError("trusted_installation_required")
        if not launcher.is_file() or not manager.is_file(): raise RuntimeError("native_helper_missing")
        plan_path=self.work/(run_id+".plan.json")
        if envelope is None:
            # Backup's manager mode accepts no plan. It is fixed and local only.
            plan_path.write_text("{}",encoding="utf-8")
        else: plan_path.write_text(json.dumps(envelope,separators=(",",":")),encoding="utf-8")
        result_path=self.work/(run_id+".result.json")
        # Remove only the exact stale per-run result before launching; never accept an earlier execution.
        result_path.unlink(missing_ok=True)
        args=["-File",launcher,"-Mode",mode,"-ScriptPath",manager,"-WorkDir",self.work,
              "-ResultPath",result_path,"-InstanceId",instance_id,"-PythonPath",sys.executable,
              "-PlanPath",plan_path,"-RunId",run_id]
        try:
            launch=self._powershell(args,timeout=600)
            if launch.returncode not in (0,None) and not result_path.is_file():
                raise RuntimeError("elevation_cancelled_or_failed")
            deadline=self.clock()+600
            while self.clock()<deadline:
                if result_path.is_file():
                    try: result=json.loads(result_path.read_text(encoding="utf-8-sig"))
                    except Exception as exc: raise RuntimeError("native_result_invalid") from exc
                    if not isinstance(result,dict) or result.get("run_id")!=run_id: raise RuntimeError("native_result_invalid")
                    if launch.returncode not in (0,None) and result.get("success") is not True: return result
                    return result
                time.sleep(.2)
            raise TimeoutError("native_helper_timeout")
        finally:
            plan_path.unlink(missing_ok=True)
