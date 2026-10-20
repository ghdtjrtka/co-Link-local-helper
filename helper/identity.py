"""Stable, public dongle identity helpers."""
from __future__ import annotations

import re
import json
import os
import subprocess
import time
import tempfile
import uuid
from pathlib import Path

SCHEMA = "co-link.dongle-identity/1"
_BAD_SERIALS = {"", "0", "00000000", "12345678", "ACTIONS1234", "UNKNOWN", "NONE"}


def make_identity(vid: int, pid: int, controller_address: str | None,
                  usb_serial: str | None = None) -> dict:
    address = (controller_address or "").replace(":", "").replace("-", "").upper()
    if not re.fullmatch(r"[0-9A-F]{12}", address) or address in {"000000000000", "FFFFFFFFFFFF"}:
        raise ValueError("stable_controller_address_missing")
    serial = (usb_serial or "").strip()
    folded = serial.upper()
    repetitive = bool(serial and (len(set(folded)) <= 2 or any(
        folded == unit * (len(folded) // len(unit)) and len(folded) % len(unit) == 0
        for unit in (folded[:1], folded[:2], folded[:3]))))
    unique = bool(serial and folded not in _BAD_SERIALS and not repetitive and
                  not re.fullmatch(r"0+", serial) and len(serial) >= 8 and len(serial) <= 128)
    return {"schema": SCHEMA, "vid": int(vid), "pid": int(pid),
            "controller_address": ":".join(address[i:i + 2] for i in range(0, 12, 2)),
            "usb_serial": serial if unique else None, "serial_unique": unique}


def inspect_identity(instance_id: str, vid: int, pid: int, *, state_root=None,
                     native_exe=None, now_ms=None, max_age_ms=30000,
                     process_alive=None, run=None, detached_reader=None):
    """Read the public controller address for one exactly selected PnP instance."""
    if not isinstance(instance_id, str) or not re.fullmatch(
            r"USB\\VID_[0-9A-Fa-f]{4}&PID_[0-9A-Fa-f]{4}(?:&MI_[0-9A-Fa-f]{2})?\\[^\x00-\x1f]{1,200}", instance_id):
        raise ValueError("invalid_instance_id")
    local = Path(state_root or os.environ.get("LOCALAPPDATA", ""))
    audio_root = local / "AudioCoDAC" / "winusb"
    transmitter_path = audio_root / "transmitter.json"
    status_path = audio_root / "bridge-status.json"
    if transmitter_path.is_file() and status_path.is_file():
        transmitter = json.loads(transmitter_path.read_text(encoding="utf-8-sig"))
        status = json.loads(status_path.read_text(encoding="utf-8-sig"))
        target = transmitter.get("instance_id")
        if target and target.casefold() == instance_id.casefold():
            pid_value = status.get("pid")
            updated_ms = status.get("updated_ms")
            current = int(time.time() * 1000) if now_ms is None else now_ms
            alive = process_alive or _pid_alive
            running = type(pid_value) is int and alive(pid_value)
            if not running:
                # The app must release USB before conversion verification. Its last
                # status is not identity evidence once stopped; read the selected
                # controller afresh through the same pinned transport instead.
                backend = transmitter.get('backend')
                if backend not in ('winusb', 'libusb0'):
                    raise ValueError('unsupported_usb_transport_backend')
                reader = detached_reader or _detached_controller_address
                controller = reader(instance_id, vid, pid, backend)
                return make_identity(vid, pid, controller)
            if (type(pid_value) is not int or type(updated_ms) is not int or
                    updated_ms > current or current - updated_ms > max_age_ms):
                # A stale status means the recorded pid is not evidence that the app still owns the
                # radio (the pid may even be reused). Read the selected controller over WinUSB
                # instead of failing the whole inspection.
                backend = transmitter.get('backend')
                if backend in ('winusb', 'libusb0'):
                    reader = detached_reader or _detached_controller_address
                    controller = reader(instance_id, vid, pid, backend)
                    return make_identity(vid, pid, controller)
                raise ValueError("converted_radio_status_stale")
            controller = status.get("controller")
            if not isinstance(controller, str):
                raise ValueError("stable_controller_address_missing")
            return make_identity(vid, pid, controller)
    native_dir = Path(__file__).parent / "native"
    candidates = [Path(native_exe)] if native_exe else [native_dir / "inspect-radio.exe",
        native_dir / "Release" / "inspect-radio.exe", native_dir / "build" / "Release" / "inspect-radio.exe"]
    executable = next((p for p in candidates if p.is_file()), candidates[0])
    if not executable.is_file():
        raise RuntimeError("native_radio_inspector_missing")
    runner = run or subprocess.run
    completed = runner([str(executable), instance_id], capture_output=True, text=True,
                       timeout=10, check=False)
    if completed.returncode != 0:
        raise RuntimeError("public_controller_address_unavailable")
    try:
        record = json.loads(completed.stdout)
    except Exception as exc:
        raise RuntimeError("native_radio_inspector_invalid_output") from exc
    return make_identity(vid, pid, record.get("controller_address"))


def _detached_controller_address(instance_id, vid, pid, backend):
    try:
        from .inspect_adapter import inspect
        from .transport_readiness import probe_transport
    except ImportError:
        from inspect_adapter import inspect
        from transport_readiness import probe_transport
    report = inspect(instance_id.upper())
    if (report.get('vid'), report.get('pid')) != (vid, pid):
        raise ValueError('selected_vid_pid_mismatch')
    with tempfile.TemporaryDirectory(prefix='co-link-identity-') as work:
        result = probe_transport(report, backend, work, uuid.uuid4().hex)
    if (result.get('status') != 'complete' or result.get('closed') is not True or
            result.get('command_responses', 0) < 3):
        raise RuntimeError('public_controller_address_unavailable')
    return result.get('controller_address')


def _pid_alive(pid):
    import ctypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]
    api.OpenProcess.restype=ctypes.c_void_p
    api.CloseHandle.argtypes=[ctypes.c_void_p]
    api.CloseHandle.restype=ctypes.c_int
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    api.CloseHandle(handle)
    return True


def main():
    import argparse
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id",required=True)
    parser.add_argument("--vid",required=True,type=lambda x:int(x,0))
    parser.add_argument("--pid",required=True,type=lambda x:int(x,0))
    args=parser.parse_args()
    try:
        print(json.dumps(inspect_identity(args.instance_id,args.vid,args.pid),separators=(",",":")))
        return 0
    except Exception:
        print('{"error":"stable_identity_unavailable"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
