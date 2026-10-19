"""Read cached USB descriptors only. Never open/claim/reset a controller or read its bond/serial."""
from __future__ import annotations
import argparse
import ctypes
import json
import platform
import re
from functools import lru_cache


@lru_cache(maxsize=1)
def _pinned_library():
    import libusb_package
    library = libusb_package.get_library_path()
    if library is None:
        raise ValueError('libusb_runtime_missing')
    loader = ctypes.WinDLL if platform.system() == 'Windows' else ctypes.CDLL
    return loader(str(library), use_errno=True, use_last_error=True)

def inspect(instance_id):
    import usb1
    match=re.fullmatch(r'USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&MI_[0-9A-F]{2})?\\[\x21-\x7e]{1,200}',instance_id)
    if not match: raise ValueError('invalid_instance_id')
    vid,pid=(int(match.group(i),16) for i in (1,2))
    # Same libusb1 initialization contract as the pinned Bumble USB transport.
    # libusb1 accepts a repeat load only for the SAME ctypes object, not merely
    # the same DLL path/handle. Preserve that object across sequential inspections.
    if not usb1.loadLibrary(_pinned_library()):
        raise ValueError('libusb_runtime_conflict')
    with usb1.USBContext() as context:
        devices=[d for d in context.getDeviceIterator(skip_on_error=True)
                 if (d.getVendorID(),d.getProductID())==(vid,pid)]
        if len(devices)!=1: raise ValueError('ambiguous_or_missing_usb_target')
        device=devices[0]
        configurations=[]
        for config in device.iterConfigurations():
            settings=[]
            for interface in config:
                for s in interface:
                    settings.append(dict(number=s.getNumber(),alternate=s.getAlternateSetting(),
                        **{'class':s.getClass()},subclass=s.getSubClass(),protocol=s.getProtocol(),
                        endpoints=[dict(address=e.getAddress(),attributes=e.getAttributes(),
                            max_packet_size=e.getMaxPacketSize(),interval=e.getInterval()) for e in s]))
            configurations.append(dict(value=config.getConfigurationValue(),settings=settings))
        return dict(schema='co-link.adapter-report/1',matches=1,instance_id=instance_id,
                    vid=vid,pid=pid,bcd_device=device.getbcdDevice(),configurations=configurations)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-id',required=True)
    parser.add_argument('--with-identity',action='store_true',help='add stable public controller identity for signed execution verification')
    args=parser.parse_args()
    try:
        report=inspect(args.instance_id.upper())
        if args.with_identity:
            from identity import inspect_identity
            report['dongle_identity']=inspect_identity(args.instance_id,report['vid'],report['pid'])
        print(json.dumps(report,ensure_ascii=True))
    except Exception:
        print('{"error":"descriptor_inspection_failed"}')
        raise SystemExit(2)
