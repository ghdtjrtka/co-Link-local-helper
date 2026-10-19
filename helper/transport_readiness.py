"""Post-bind verification using the SAME pinned HCI initialization as the desktop.

Used after signed binding/recheck and to recover a selected, already-converted
controller's public address after its app releases USB. Cached descriptor-only
inspection does not call this. No pairing or audio test is started.
A descriptor/service match is not readiness.
"""
from pathlib import Path
import importlib.util
import json
import re
import subprocess
import sys
import uuid


_INSTANCE = re.compile(
    r"USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})(?:&MI_([0-9A-Fa-f]{2}))?\\[^\\\s]+\Z"
)


def _load_usb_target_parser(script):
    """Load the packaged verifier's parser without initializing its USB backend."""
    name = "_co_link_radio_probe_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError('packaged_transport_verifier_invalid')
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves its defining module through sys.modules.
    import sys as _sys
    _sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module.load_usb_target
    except Exception:
        _sys.modules.pop(name, None)
        raise
    finally:
        _sys.modules.pop(name, None)


def probe_transport(report, backend, work, job_id, *, runner=subprocess.run):
    root = Path(__file__).resolve().parent
    script = root / 'radio' / 'winusb_radio_probe.py'
    if not script.is_file():
        raise RuntimeError('packaged_transport_verifier_missing')
    instance_id = report.get('instance_id')
    match = _INSTANCE.fullmatch(instance_id.upper()) if isinstance(instance_id, str) else None
    if not match:
        raise ValueError('invalid_selected_usb_instance')
    try:
        report_vid, report_pid = int(report['vid']), int(report['pid'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('invalid_selected_usb_ids') from exc
    if (report_vid, report_pid) != (int(match[1], 16), int(match[2], 16)):
        raise ValueError('selected_vid_pid_mismatch')
    if backend not in ('winusb', 'libusb0'):
        raise ValueError('unsupported_usb_transport_backend')

    # load_usb_target currently requires a supplied serial to equal the PnP
    # instance suffix. MI child suffixes are not guaranteed to be the physical
    # USB serial, so retain dongle identity separately and pass serial only
    # when the validated report proves that this parser's condition holds.
    serial = ''
    identity = report.get('dongle_identity')
    if isinstance(identity, dict):
        if (identity.get('vid'), identity.get('pid')) != (report_vid, report_pid):
            raise ValueError('dongle_identity_vid_pid_mismatch')
        if identity.get('serial_unique') is True:
            candidate = identity.get('usb_serial')
            if not isinstance(candidate, str) or not candidate.strip():
                raise ValueError('invalid_verified_usb_serial')
            if candidate.casefold() == instance_id.rsplit('\\', 1)[-1].casefold():
                serial = candidate

    target = Path(work) / (job_id + '.probe-target.json')
    target.write_text(json.dumps({'instance_id': instance_id, 'vid': report_vid,
        'pid': report_pid, 'backend': backend,
        'interface': int(match[3], 16) if match[3] else -1, 'serial': serial}), encoding='utf8')
    try:
        # Validate with the exact parser shipped beside the verifier before any
        # runner call (which may open the selected USB transport and issue HCI).
        try:
            _load_usb_target_parser(script)(target)
        except Exception as exc:
            raise ValueError('invalid_selected_usb_target') from exc
        result = runner([sys.executable, '-B', str(script), '--initialize', '--target', str(target)],
                        capture_output=True, text=True, timeout=50, check=False)
        raw = json.loads(result.stdout)
        buffers = raw.get('buffer_size') or {}
        # Bumble's typed public address string includes /P. The cloud identity
        # uses the canonical six-octet controller address without that type tag.
        address = str(raw.get('bd_addr') or '').upper()
        if address.endswith('/P'):
            address = address[:-2]
        valid_address = re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address)
        complete = (result.returncode == 0 and raw.get('status') == 'complete' and
                    raw.get('closed') is True and raw.get('device_powered_on') is True and
                    not raw.get('cleanup_errors') and not raw.get('close_error') and bool(valid_address))
        return {'status': 'complete' if complete else 'failed', 'closed': raw.get('closed') is True,
                'controller_address': address if valid_address else '',
                'command_responses': raw.get('hci_command_responses', 0),
                'acl_packet_length': buffers.get('acl_packet_length', 0),
                'total_acl_packets': buffers.get('total_acl_packets', 0)}
    finally:
        target.unlink(missing_ok=True)
