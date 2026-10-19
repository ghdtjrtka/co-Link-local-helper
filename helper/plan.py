"""Verify the exact cloud-signed bytes before any native conversion. No device access here."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import re
import time
from pathlib import Path

POLICY = 'usb-layout-20261018-v2'
HELPERS = {
    'winusb': 'E5C3C51396E5AEACC5B63D2991623894EB5A6A8981B7AEF680E1D0D8AF8A4387',
    'libusb0': '199EFD67133FBD1B58F7698498F4FB3975E4A3C5EDA820347B1C72329D260277',
}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')

def report_identity(report, descriptors_only=False):
    keys = ['vid', 'pid', 'bcd_device', 'configurations']
    if not descriptors_only:
        keys += ['schema', 'instance_id']
    return {k:report[k] for k in keys}

def fingerprint(report, descriptors_only=False):
    return hashlib.sha256(canonical(report_identity(report, descriptors_only))).hexdigest()

def dongle_fingerprint(report):
    identity=report.get('dongle_identity')
    if not isinstance(identity,dict) or identity.get('schema')!='co-link.dongle-identity/1':
        raise ValueError('stable_identity_required')
    address=str(identity.get('controller_address','')).upper()
    if (identity.get('vid'),identity.get('pid'))!=(report['vid'],report['pid']) or not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}',address) or address in ['00:00:00:00:00:00','FF:FF:FF:FF:FF:FF']:
        raise ValueError('invalid_stable_identity')
    serial=identity.get('usb_serial') if identity.get('serial_unique') is True else None
    if serial is not None and (not isinstance(serial,str) or not 0<len(serial)<=200 or serial.upper() in ['ACTIONS1234','00000000','12345678']):
        raise ValueError('invalid_unique_serial')
    return hashlib.sha256(canonical(dict(schema=identity['schema'],vid=identity['vid'],pid=identity['pid'],
        controller_address=address,usb_serial=serial))).hexdigest()

def verify(envelope, keys, report, now=None):
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes
    now = int(time.time()) if now is None else now
    if envelope.get('alg') != 'RS256' or envelope.get('key_id') not in keys.get('keys', {}):
        raise ValueError('untrusted_plan_key')
    raw = base64.b64decode(envelope['payload_b64'], validate=True)
    signature = base64.b64decode(envelope['signature_b64'], validate=True)
    if len(raw) > 8192 or len(signature) > 1024:
        raise ValueError('plan_too_large')
    jwk = keys['keys'][envelope['key_id']]
    def number(v):
        return int.from_bytes(base64.urlsafe_b64decode(v + '=' * (-len(v) % 4)), 'big')
    key = rsa.RSAPublicNumbers(number(jwk['e']), number(jwk['n'])).public_key()
    if key.key_size < 2048:
        raise ValueError('weak_plan_key')
    key.verify(signature, raw, padding.PKCS1v15(), hashes.SHA256())
    plan = json.loads(raw)
    if plan.get('schema') == 'co-link.recovery-plan/1':
        allowed = {'schema','operation','job_id','instance_id','dongle_fingerprint','backup_id',
                   'backup_sha256','original_service','original_inf','issued_at','expires_at'}
        if (set(plan) != allowed or plan['operation'] != 'restore-original' or
                not re.fullmatch(r'[a-f0-9]{32}', str(plan['job_id'])) or
                not re.fullmatch(r'[a-f0-9]{64}', str(plan['backup_sha256'])) or
                plan['instance_id'] != report['instance_id'] or
                plan['dongle_fingerprint'] != dongle_fingerprint(report)):
            raise ValueError('recovery_plan_mismatch')
        if (type(plan['issued_at']) is not int or type(plan['expires_at']) is not int or
                plan['issued_at'] > now+30 or plan['expires_at'] <= now or
                not 0 < plan['expires_at']-plan['issued_at'] <= 600):
            raise ValueError('plan_expired_or_invalid_time')
        return plan
    allowed = {'schema','job_id','issued_at','expires_at','operation','policy_version','qualification_id',
               'instance_id','fingerprint','descriptor_sha256','backend','helper_sha256',
               'dongle_fingerprint','entitlement_id','scope','backup_id','backup_sha256'}
    if set(plan) != allowed or plan['schema'] != 'co-link.conversion-plan/3' or plan['operation'] not in ('bind-transmitter','verify-transmitter'):
        raise ValueError('unsupported_plan')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',str(plan['backup_id'])) or not re.fullmatch(r'[a-f0-9]{64}',str(plan['backup_sha256'])):
        raise ValueError('invalid_plan_backup')
    if plan['policy_version'] != POLICY or plan['backend'] not in HELPERS or plan['helper_sha256'] != HELPERS[plan['backend']]:
        raise ValueError('unsupported_plan_backend')
    if (type(plan['issued_at']) is not int or type(plan['expires_at']) is not int or
            plan['issued_at'] > now+30 or plan['expires_at'] <= now or
            not 0 < plan['expires_at']-plan['issued_at'] <= 600):
        raise ValueError('plan_expired_or_invalid_time')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', plan['job_id']):
        raise ValueError('invalid_job_id')
    if (plan['scope']!='dongle-lifetime' or not re.fullmatch(r'[a-f0-9]{32}',str(plan['entitlement_id'])) or
            plan['dongle_fingerprint']!=dongle_fingerprint(report)):
        raise ValueError('plan_entitlement_mismatch')
    if (report.get('schema') != 'co-link.adapter-report/1' or report.get('matches') != 1 or
            plan['instance_id'] != report['instance_id'] or
            plan['fingerprint'] != fingerprint(report) or plan['descriptor_sha256'] != fingerprint(report, True)):
        raise ValueError('plan_device_mismatch')
    return plan

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--keys', type=Path, default=Path(__file__).with_name('plan-public-keys.json'))
    args=parser.parse_args()
    try:
        for path in [args.plan,args.report,args.keys]:
            if path.stat().st_size > 65536: raise ValueError('input_too_large')
        result=verify(json.loads(args.plan.read_text(encoding='utf-8-sig')),
                      json.loads(args.keys.read_text(encoding='utf-8-sig')),
                      json.loads(args.report.read_text(encoding='utf-8-sig')))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        # No raw report, signed payload or configuration secrets in an error log.
        print('{"error":"plan_verification_failed"}')
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
