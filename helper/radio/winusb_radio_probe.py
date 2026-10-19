#!/usr/bin/env python3
"""Descriptor preflight and opt-in source-based controller bring-up for one dongle.

The default command only enumerates cached USB descriptors; it never opens the
selected radio, claims its interface, or sends HCI. ``--target`` accepts the same exact
adapter selection as the app (required), using Bumble's pinned
USB packet classes and its HCI Host/Device and known Realtek/Intel loaders.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import importlib
import json
import logging
import sys
import threading
import traceback
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


BUMBLE_PINNED_COMMIT = "a679beb4f36681c6f1b6d72b7e825c6f57f9ef83"
INITIALIZE_TIMEOUT_SECONDS = 18.0
HCI_COMMAND_TIMEOUT_SECONDS = 12.0
LEGACY_API_SHA256 = '0690cd87d0069edac3a867e0b51f379afa77159eda3b45cd02740fc6afb546d8'
LEGACY_TRANSPORT_SHA256 = '2e1c3482ff8653439a9eecbafc3128ae13948bd6668a677e9c1aec49685160eb'
_legacy_usb_api = None
_legacy_transport_api = None


@dataclass(frozen=True)
class UsbTarget:
    vid: int
    pid: int
    serial: str = ""
    interface: int = -1
    backend: str = 'winusb'


def load_usb_target(path: Path) -> UsbTarget:
    """Load the exact adapter selected by the app, never a first-available USB device."""
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    match = re.fullmatch(r'USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&MI_([0-9A-F]{2}))?\\[^\\\s]+',
                         value['instance_id'].upper())
    if not match:
        raise ProbeError('invalid selected USB instance')
    backend = value.get('backend', 'winusb')
    if backend not in ('winusb', 'libusb0'):
        raise ProbeError('unsupported USB transport backend')
    target = UsbTarget(int(value['vid']), int(value['pid']), value.get('serial', ''),
                       int(value.get('interface', -1)), backend)
    if (target.vid, target.pid) != (int(match[1], 16), int(match[2], 16)):
        raise ProbeError('selected VID/PID does not match its instance')
    interface = int(match[3], 16) if match[3] else -1
    if target.interface != interface or not isinstance(target.serial, str):
        raise ProbeError('invalid selected USB interface or serial')
    if target.serial and target.serial.upper() != value['instance_id'].rsplit('\\', 1)[-1].upper():
        raise ProbeError('selected serial does not match its instance')
    return target


class ProbeError(RuntimeError):
    """A safe, user-facing failure reason (never includes packet/key data)."""


class CloseFailure(RuntimeError):
    def __init__(self, errors: list[Exception]):
        super().__init__("one or more USB transport close operations failed")
        self.errors = errors


class ProbeRunState:
    """Small cross-thread status ledger; never stores HCI packet contents."""

    def __init__(self):
        self.phase = "open"
        self.device_opened = False
        self.transport_opened = False
        self.device_powered_on = False
        self.closed = False
        self.identity_verified = False
        self.unique_candidate = False
        self.hci_commands_enqueued = 0
        self.hci_command_transfers_completed = 0
        self.hci_command_transfers_failed = 0
        self.hci_command_responses = 0
        self.hci_opcodes: list[int] = []
        self.close_errors: list[dict[str, Any]] = []
        self._pending_command_transfers = 0
        self._lock = threading.Lock()

    def command_enqueued(self, opcode: int | None = None) -> None:
        with self._lock:
            self.hci_commands_enqueued += 1
            self._pending_command_transfers += 1
            if opcode is not None:
                self.hci_opcodes.append(opcode)

    def command_transfer_result(self, completed: bool) -> None:
        with self._lock:
            if self._pending_command_transfers <= 0:
                return
            self._pending_command_transfers -= 1
            if completed:
                self.hci_command_transfers_completed += 1
            else:
                self.hci_command_transfers_failed += 1

    def command_response_received(self) -> None:
        with self._lock:
            self.hci_command_responses += 1

    def record_close_error(self, error: Exception) -> None:
        self.close_errors.append(exception_details(error, "close"))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            completed = self.hci_command_transfers_completed
            return {
                "phase": self.phase,
                "device_opened": self.device_opened,
                "transport_opened": self.transport_opened,
                "device_powered_on": self.device_powered_on,
                "closed": self.closed,
                "serial_verified": self.identity_verified,
                "unique_candidate": self.unique_candidate,
                "hci_commands_enqueued": self.hci_commands_enqueued,
                "hci_command_transfers_completed": completed,
                "hci_command_transfers_failed": self.hci_command_transfers_failed,
                "hci_command_responses": self.hci_command_responses,
                "hci_opcodes": [f'0x{opcode:04X}' for opcode in self.hci_opcodes],
                # USB transfer completion proves a control transfer completed;
                # command-response count separately proves HCI command acceptance.
                "hci_command_sent": completed > 0,
                "close_errors": list(self.close_errors),
            }


def exception_details(error: Exception, phase: str) -> dict[str, Any]:
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    last = frames[-1] if frames else None
    detail: dict[str, Any] = {
        "phase": phase,
        "type": type(error).__name__,
        "errno": getattr(error, "errno", None),
        "winerror": getattr(error, "winerror", None),
        "last_traceback": None
        if last is None
        else {
            "filename": Path(last.filename).name,
            "line": int(last.lineno),
            "function": last.name,
        },
    }
    if isinstance(error, ProbeError):
        detail["reason"] = str(error)  # ProbeError messages are fixed, non-secret text.
    if isinstance(error, CloseFailure):
        detail["close_errors"] = [exception_details(item, "close") for item in error.errors]
    return detail


def _load_bumble_usb():
    """Import the checked-in Bumble source, not an unrelated installed copy."""
    from bumble_runtime import ensure_bumble_source
    try:
        ensure_bumble_source()
    except ImportError as error:
        raise ProbeError(str(error)) from None
    try:
        return importlib.import_module("bumble.transport.usb")
    except Exception as error:
        raise ProbeError(f"Bumble USB backend unavailable ({type(error).__name__})") from None


def _new_context(usb_api, run_state: ProbeRunState | None = None,
                 library_loader: Callable[[], None] | None = None):
    context = None
    try:
        (library_loader or usb_api.load_libusb)()
        context = usb_api.usb1.USBContext()
        context.open()
        return context
    except Exception:
        if context is not None:
            try:
                context.close()
            except Exception as close_error:
                if run_state is not None:
                    run_state.record_close_error(close_error)
        raise


def _load_legacy_usb_api():
    """Supply libusb1's existing libusb0 adapter from one pinned local DLL.

    Preload before libusb_init; keep it alive for the transport lifetime. No PATH,
    system DLL, filter or kernel settings are changed by loading the user API.
    """
    global _legacy_usb_api
    if _legacy_usb_api is not None:
        return
    path = Path(__file__).resolve().parent / 'vendor' / 'libusbK.dll'
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != LEGACY_API_SHA256:
        raise ProbeError('legacy USB user API is missing or its pinned hash differs')
    if sys.platform != 'win32' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ProbeError('legacy USB transport currently requires 64-bit Windows')
    _legacy_usb_api = ctypes.WinDLL(str(path))


def _load_legacy_transport(usb_api):
    """Use the upstream transfer engine with an explicit pinned user-API handle.

    Upstream1.0.26 otherwise searches System32 exclusively for libusbK. The local
    extension acquires a reference to this exact module; its transfer logic stays
    unchanged. usb1 only accepts its first DLL, so reject any earlier different one.
    """
    global _legacy_transport_api
    _load_legacy_usb_api()
    if _legacy_transport_api is None:
        path = Path(__file__).resolve().parent / 'vendor' / 'libusb-1.0-legacy.dll'
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != LEGACY_TRANSPORT_SHA256:
            raise ProbeError('legacy USB transport is missing or its pinned hash differs')
        dll = ctypes.WinDLL(str(path), use_errno=True, use_last_error=True)
        setter = dll.libusb_codac_set_legacy_api
        setter.argtypes, setter.restype = [ctypes.c_void_p], ctypes.c_int
        if setter(ctypes.c_void_p(_legacy_usb_api._handle)) != 0:
            raise ProbeError('legacy USB API handle was rejected before context creation')
        _legacy_transport_api = dll
    if not usb_api.usb1.loadLibrary(_legacy_transport_api):
        raise ProbeError('a different USB library was loaded before the selected legacy transport')


def _bus_port_moniker(device) -> str:
    """Bumble's USB selector form, avoiding its VID selector's global serial scan."""
    ports = ".".join(str(port) for port in device.getPortNumberList())
    if not ports:
        raise ProbeError("target USB device has no stable port path")
    return f"usb:{device.getBusNumber()}-{ports}"


def _endpoint_summary(usb_api, device) -> dict[str, Any]:
    endpoints = usb_api.find_endpoints(device, False, None)
    if endpoints is None:
        return {"standard_hci_endpoints": False}

    (
        configuration,
        acl_interface,
        _sco_interface,
        interrupt_in,
        bulk_in,
        _isochronous_in,
        bulk_out,
        _isochronous_out,
    ) = endpoints
    interface_tuple = (
        acl_interface.getClass(),
        acl_interface.getSubClass(),
        acl_interface.getProtocol(),
    )
    expected_tuple = tuple(usb_api.USB_BT_HCI_CLASS_TUPLE)
    is_standard = interface_tuple == expected_tuple
    return {
        "standard_hci_endpoints": bool(is_standard),
        "configuration": int(configuration),
        "interface": int(acl_interface.getNumber()),
        "alternate_setting": int(acl_interface.getAlternateSetting()),
        "interface_class_tuple": [int(value) for value in interface_tuple],
        "interrupt_in": int(interrupt_in.getAddress()),
        "bulk_in": int(bulk_in.getAddress()),
        "bulk_out": int(bulk_out.getAddress()),
    }


def validate_usb_configurations(device) -> list[dict[str, Any]]:
    """Check cached interface topology before any driver change or device open.

    VID/PID and the presence of HCI endpoints do not establish a valid USB
    configuration. The KLCOM sample repeats interface 1 / alternate 5. Preserve
    that finding, rather than silently choosing one of the two descriptors.
    """
    issues: list[dict[str, Any]] = []
    for configuration in device.iterConfigurations():
        config_id = int(configuration.getConfigurationValue())
        seen: set[tuple[int, int]] = set()
        counts: dict[int, int] = {}
        for interface in configuration:
            for setting in interface:
                key = (int(setting.getNumber()), int(setting.getAlternateSetting()))
                counts[key[0]] = counts.get(key[0], 0) + 1
                if key in seen:
                    issues.append({'code': 'duplicate_alternate_setting',
                                   'configuration': config_id, 'interface': key[0], 'alternate_setting': key[1]})
                seen.add(key)
        # WDF FxUsbInterface::CreateSettings indexes a descriptor-count-sized
        # table by bAlternateSetting, rejecting holes/out-of-range settings.
        for number, count in sorted(counts.items()):
            actual = {alt for interface, alt in seen if interface == number}
            if actual != set(range(count)):
                issues.append({'code': 'noncontiguous_alternate_settings', 'configuration': config_id,
                               'interface': number, 'descriptor_count': count,
                               'missing': sorted(set(range(count)) - actual)})
    return issues


def usb_backend_profile(device) -> tuple[str, str, list[dict[str, Any]]]:
    """Choose the transport from its actual USB layout, retaining malformed data.

    libusb0's inspected WDM setup selects alternate0 only. Restrict that treatment
    to a two-interface HCI/SCO device with valid HCI0 and errors confined to the
    unused isochronous SCO1 settings. No descriptor or firmware bytes are patched.
    A returned backend is a preparation candidate, not proof of radio operation.
    """
    issues = validate_usb_configurations(device)
    if not issues:
        return 'winusb', 'standard-hci', issues
    allowed = {'duplicate_alternate_setting', 'noncontiguous_alternate_settings'}
    if any(x['code'] not in allowed or x.get('interface') != 1 for x in issues):
        return '', 'unsupported-usb-layout', issues
    configs = list(device.iterConfigurations())
    if len(configs) != 1:
        return '', 'unsupported-usb-layout', issues
    settings = [s for interface in configs[0] for s in interface]
    hci = [s for s in settings if s.getNumber() == 0]
    sco = [s for s in settings if s.getNumber() == 1]
    if (len(hci) != 1 or hci[0].getAlternateSetting() != 0 or not sco
            or len(settings) != len(hci) + len(sco)):
        return '', 'unsupported-usb-layout', issues
    for setting in settings:
        if (setting.getClass(), setting.getSubClass(), setting.getProtocol()) != (0xE0, 1, 1):
            return '', 'unsupported-usb-layout', issues
    if not any(s.getAlternateSetting() == 0 for s in sco):
        return '', 'unsupported-usb-layout', issues
    for setting in sco:
        endpoints = list(setting)
        if (len(endpoints) != 2 or any((e.getAttributes() & 3) != 1 for e in endpoints)
                or {e.getAddress() & 0x80 for e in endpoints} != {0, 0x80}):
            return '', 'unsupported-usb-layout', issues
    return 'libusb0', 'hci-valid-sco-alternate-quirk', issues


def enumerate_usb_descriptors(usb_api=None, run_state: ProbeRunState | None = None,
                              target: UsbTarget | None = None) -> dict[str, Any]:
    """Read USB descriptor metadata without opening a device or reading serials."""
    if target is None:
        raise ProbeError('explicit transmitter selection is required')
    usb_api = usb_api or _load_bumble_usb()
    context = _new_context(usb_api, run_state)
    matches: list[dict[str, Any]] = []
    try:
        for device in context.getDeviceIterator(skip_on_error=True):
            try:
                vid = int(device.getVendorID())
                pid = int(device.getProductID())
                if (vid, pid) != (target.vid, target.pid):
                    continue
                try:
                    endpoints = _endpoint_summary(usb_api, device)
                    backend, profile, configuration_issues = usb_backend_profile(device)
                    moniker = _bus_port_moniker(device)
                    bus = int(device.getBusNumber())
                except Exception as error:
                    endpoints = {"standard_hci_endpoints": False}
                    moniker = None
                    bus = None
                    descriptor_error = type(error).__name__
                    configuration_issues = [{'code': 'descriptor_read_failed'}]
                    backend, profile = '', 'descriptor-read-failed'
                else:
                    descriptor_error = None
                matches.append(
                    {
                        "vid": f"{vid:04X}",
                        "pid": f"{pid:04X}",
                        "bcd_device": int(device.getbcdDevice()),
                        "bus": bus,
                        "bus_port_moniker": moniker,
                        "serial": "not-read (preflight does not open devices)",
                        "serial_verified": False,
                        "descriptor_error": descriptor_error,
                        "configuration_issues": configuration_issues,
                        "recommended_backend": backend,
                        "profile": profile,
                        "controller_init": _controller_init_plan(vid, pid),
                        **endpoints,
                    }
                )
            finally:
                # usb1's enumerated USBDevice is only a descriptor reference here.
                # Do not call .open(), getSerialNumber(), or claim an interface.
                try:
                    device.close()
                except Exception:
                    pass
    finally:
        context.close()

    return {
        "mode": "preflight",
        "bumble_commit": BUMBLE_PINNED_COMMIT,
        "target": {
            "vid": f"{target.vid:04X}",
            "pid": f"{target.pid:04X}",
            "expected_serial": target.serial,
        },
        "candidate_count": len(matches),
        "unique_vid_pid_candidate": len(matches) == 1,
        "device_opened": False,
        "hci_sent": False,
        "candidates": matches,
    }


def _controller_init_plan(vid: int, pid: int) -> dict[str, Any]:
    # Runtime policy is sourced from the pinned upstream USB tables. A loader
    # candidate is distinct from verified radio/audio operation.
    from usb_chipset_init import describe_controller_init
    return describe_controller_init(vid, pid)


class OwnedBumbleTransport:
    """Keep usb1's USBDevice descriptor owner alive as long as its handle."""

    def __init__(self, transport, descriptor_device):
        self._transport = transport
        self._descriptor_device = descriptor_device
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._transport, name)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        errors = []
        transport_closed = False
        try:
            await self._transport.close()
            transport_closed = True
        except Exception as error:
            errors.append(error)
        try:
            # usb1 USBDevice.close() also finalizes every USBDeviceHandle it
            # created. Do this only after Bumble has finished using its handle.
            self._descriptor_device.close()
        except Exception as error:
            errors.append(error)
        if not transport_closed:
            try:
                self._transport.context.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise CloseFailure(errors)


def open_selected_bumble_transport(usb_api=None, run_state: ProbeRunState | None = None,
                                  target: UsbTarget | None = None):
    """Open only the unique target, selected by Bumble's bus-port moniker.

    Bumble's convenience open_usb_transport('10d7:b012') reads serial strings
    from every enumerated USB device before VID/PID filtering and requests
    auto-detach.  This scoped construction instead uses the same Bumble
    endpoint/packet/transport classes, rechecks the selected bus-port path and
    VID/PID, reads serial only for that target, and never requests driver detach.
    """
    if target is None:
        raise ProbeError('explicit transmitter selection is required')
    real_legacy = usb_api is None and target.backend == 'libusb0'
    usb_api = usb_api or _load_bumble_usb()
    if run_state is not None:
        run_state.phase = "open"
    context = _new_context(usb_api, run_state,
                           (lambda: _load_legacy_transport(usb_api)) if real_legacy else None)
    candidates = []
    selected_device = None
    device_handle = None
    succeeded = False
    try:
        for device in context.getDeviceIterator(skip_on_error=True):
            try:
                if (int(device.getVendorID()), int(device.getProductID())) == (target.vid, target.pid):
                    candidates.append(device)
                else:
                    device.close()
            except Exception:
                try:
                    device.close()
                except Exception:
                    pass
        if len(candidates) != 1:
            raise ProbeError("refusing initialization: target VID/PID is not unique")
        if run_state is not None:
            run_state.unique_candidate = True

        selected_device = candidates[0]
        backend, profile, issues = usb_backend_profile(selected_device)
        if not backend or target.backend != backend:
            raise ProbeError('USB layout requires a different explicitly selected transport backend')
        selected_path = _bus_port_moniker(selected_device)
        if target.serial:
            try:
                serial = selected_device.getSerialNumber()
            except Exception:
                raise ProbeError("refusing initialization: target serial descriptor could not be read") from None
            if not serial or serial.upper() != target.serial.upper():
                raise ProbeError("refusing initialization: selected serial does not match")
        if run_state is not None:
            run_state.identity_verified = True
        # Keep using this exact descriptor object (and its bus-port moniker) for
        # open. Re-enumerating or calling USBDevice.close() after open would
        # finalize usb1's handle, because USBDevice.close closes owned handles.
        endpoints = usb_api.find_endpoints(selected_device, False, None)
        if endpoints is None:
            raise ProbeError("refusing initialization: target has no standard Bluetooth HCI endpoint set")
        (
            configuration,
            acl_interface,
            sco_interface,
            interrupt_in,
            bulk_in,
            isochronous_in,
            bulk_out,
            isochronous_out,
        ) = endpoints
        if target.backend == 'libusb0':
            # This profile carries BR/EDR ACL/HID/A2DP only. Never claim or select
            # malformed SCO alternate settings merely because libusb lists them.
            sco_interface = isochronous_in = isochronous_out = None
        if (
            acl_interface.getClass(),
            acl_interface.getSubClass(),
            acl_interface.getProtocol(),
        ) != tuple(usb_api.USB_BT_HCI_CLASS_TUPLE):
            raise ProbeError("refusing initialization: target interface is not standard USB HCI")
        if target.interface >= 0 and acl_interface.getNumber() != target.interface:
            raise ProbeError('selected HCI interface does not match')

        device_handle = selected_device.open()
        if run_state is not None:
            run_state.device_opened = True
        try:
            current_configuration = int(device_handle.getConfiguration())
        except Exception:
            current_configuration = 0
        if current_configuration != int(configuration):
            # Bumble's standard USB configuration selection, scoped to the
            # approved target. No vendor request or driver auto-detach is used.
            device_handle.setConfiguration(int(configuration))

        source = usb_api.UsbPacketSource(
            device_handle,
            {"vendor_id": target.vid, "product_id": target.pid},
            interrupt_in,
            bulk_in,
            isochronous_in,
        )
        class ProbePacketSink(usb_api.UsbPacketSink):
            def transfer_callback(self, transfer):
                if run_state is not None:
                    try:
                        completed = transfer.getStatus() == usb_api.usb1.TRANSFER_COMPLETED
                        run_state.command_transfer_result(completed)
                    except Exception:
                        # The HCI response counter remains authoritative when
                        # this backend cannot report transfer status.
                        pass
                return super().transfer_callback(transfer)

        sink = ProbePacketSink(
            device_handle,
            bulk_out,
            isochronous_out,
            acl_interface.getNumber(),
        )
        transport = usb_api.UsbTransport(
            context,
            device_handle,
            acl_interface,
            sco_interface,
            source,
            sink,
        )
        owned_transport = OwnedBumbleTransport(transport, selected_device)
        if run_state is not None:
            run_state.transport_opened = True
        device_handle = None  # Bumble UsbTransport owns it; descriptor owner is retained.
        context = None
        succeeded = True
        return owned_transport
    finally:
        descriptor_owner_closed = False
        if not succeeded:
            for device in candidates:
                try:
                    device.close()
                    descriptor_owner_closed = True
                except Exception as close_error:
                    if run_state is not None:
                        run_state.record_close_error(close_error)
        if device_handle is not None and not descriptor_owner_closed:
            try:
                device_handle.close()
            except Exception as close_error:
                if run_state is not None:
                    run_state.record_close_error(close_error)
        if context is not None:
            try:
                context.close()
            except Exception as close_error:
                if run_state is not None:
                    run_state.record_close_error(close_error)


def _make_bumble_device(transport, run_state: ProbeRunState | None = None):
    try:
        from bumble import hci
        from bumble.device import Device, DeviceConfiguration
        from bumble.host import Host
        from bumble.keys import MemoryKeyStore
    except Exception as error:
        raise ProbeError(f"pinned Bumble HCI stack unavailable ({type(error).__name__})") from None

    class ProbeHost(Host):
        def __init__(self, source, sink):
            super().__init__(controller_source=source, controller_sink=sink)
            self.read_buffer_size = None
            self.read_bd_addr = None
            self.applied_quirks: list[str] = []

        def supports_command(self, op_code):
            version = self.local_version
            # This exact observed controller answered page0 but never page1 of
            # Read_Local_Extended_Features (two bounded live captures). Select
            # Host.reset's existing mandatory basic-feature read instead. No
            # HCI completion or unsupported upper-page capability is fabricated.
            if (op_code == hci.HCI_READ_LOCAL_EXTENDED_FEATURES_COMMAND
                    and self.hci_metadata.get('vendor_id') == 0x0A12
                    and self.hci_metadata.get('product_id') == 0x0001
                    and version is not None
                    and (version.company_identifier, version.hci_subversion,
                         version.lmp_subversion, version.hci_version) == (0x08E7, 0x0089, 0x0089, 0x0A)):
                name = '0a12-0001-barrot-0089-basic-feature-query'
                if name not in self.applied_quirks:
                    self.applied_quirks.append(name)
                return False
            return super().supports_command(op_code)

        async def reset(self, driver_factory=None):
            from usb_chipset_init import controller_driver_factory
            return await super().reset(driver_factory=controller_driver_factory)

        async def send_sync_command(self, command, response_timeout=None):
            response = await super().send_sync_command(command, response_timeout)
            if run_state is not None:
                run_state.command_response_received()
            if isinstance(command, hci.HCI_Read_Buffer_Size_Command):
                self.read_buffer_size = response
            elif isinstance(command, hci.HCI_Read_BD_ADDR_Command):
                self.read_bd_addr = response
            return response

    config = DeviceConfiguration(
        le_enabled=False,
        classic_enabled=False,
        classic_smp_enabled=False,
        connectable=False,
        discoverable=False,
        keystore=None,
    )
    class ObservedCommandSink:
        def __init__(self, sink):
            self._sink = sink

        def on_packet(self, packet):
            # Count only after Bumble's USB sink accepted the HCI packet into
            # its queue. Completion is recorded by the selected USB sink below.
            self._sink.on_packet(packet)
            if run_state is not None and packet and packet[0] == hci.HCI_COMMAND_PACKET:
                run_state.command_enqueued(int.from_bytes(packet[1:3], 'little') if len(packet) >= 3 else None)

    observed_sink = ObservedCommandSink(transport.sink)
    host = ProbeHost(transport.source, observed_sink)
    device = Device(config=config, host=host)
    # Keep Bumble's default in-memory key store; never load a file or Windows bond.
    device.keystore = MemoryKeyStore()
    device.command_timeout = HCI_COMMAND_TIMEOUT_SECONDS
    return device, host


def _hci_result(device, host) -> dict[str, Any]:
    version = getattr(host, 'local_version', None)
    buffer_size = getattr(host, 'read_buffer_size', None)
    bd_addr = getattr(host, 'read_bd_addr', None)
    return {
        "local_version": None
        if version is None
        else {
            "hci_version": int(version.hci_version),
            "hci_subversion": int(version.hci_subversion),
            "lmp_version": int(version.lmp_version),
            "company_identifier": int(version.company_identifier),
            "lmp_subversion": int(version.lmp_subversion),
        },
        "buffer_size": None
        if buffer_size is None
        else {
            "acl_packet_length": int(buffer_size.hc_acl_data_packet_length),
            "synchronous_packet_length": int(buffer_size.hc_synchronous_data_packet_length),
            "total_acl_packets": int(buffer_size.hc_total_num_acl_data_packets),
            "total_synchronous_packets": int(buffer_size.hc_total_num_synchronous_data_packets),
        },
        "bd_addr": None if bd_addr is None else str(bd_addr.bd_addr),
        "device_model": None,
        "model_inference": False,
        "applied_quirks": getattr(host, 'applied_quirks', []),
        "controller_init": getattr(host, 'controller_init_strategy', None),
    }


async def initialize_with_transport(
    transport_factory: Callable[[ProbeRunState], Any],
    device_factory: Callable[[Any, ProbeRunState], tuple[Any, Any]],
    timeout_seconds: float = INITIALIZE_TIMEOUT_SECONDS,
    run_state: ProbeRunState | None = None,
    target: UsbTarget | None = None,
) -> dict[str, Any]:
    """Run standard initialization and preserve the primary and close errors."""
    if target is None:
        raise ProbeError('explicit transmitter selection is required')
    run_state = run_state or ProbeRunState()
    transport = None
    hci_result = None
    original_error = None
    original_error_phase = None
    transport_close_error = None
    device = host = None
    try:
        run_state.phase = "open"
        transport = transport_factory(run_state)
        run_state.transport_opened = True
        run_state.phase = "power_on"
        device, host = device_factory(transport, run_state)
        await asyncio.wait_for(device.power_on(), timeout=timeout_seconds)
        run_state.device_powered_on = True
        run_state.phase = "collect"
        hci_result = _hci_result(device, host)
    except Exception as error:
        original_error = error
        original_error_phase = run_state.phase
        if host is not None:
            # Preserve successfully read identity/buffers even when a later
            # command hangs. A partial read is not completed initialization.
            hci_result = _hci_result(device, host)
    finally:
        if transport is not None:
            run_state.phase = "close"
            try:
                await transport.close()
                run_state.closed = True
            except Exception as error:
                transport_close_error = error

    result: dict[str, Any] = {
        "mode": "initialize",
        "bumble_commit": BUMBLE_PINNED_COMMIT,
        "target": {
            "vid": f"{target.vid:04X}",
            "pid": f"{target.pid:04X}",
            "serial": target.serial,
            "serial_verified": run_state.identity_verified,
            "unique": run_state.unique_candidate,
        },
        "pairing_started": False,
        "vendor_commands_sent": any(opcode >> 10 == 0x3F for opcode in run_state.hci_opcodes),
        **run_state.snapshot(),
    }
    if hci_result is not None:
        result.update(hci_result)
    if original_error is not None:
        result["status"] = "timeout" if isinstance(original_error, (TimeoutError, asyncio.TimeoutError)) else "failed"
        result["error"] = exception_details(original_error, original_error_phase or "open")
    elif transport_close_error is not None:
        result["status"] = "close_failed"
        result["close_error"] = exception_details(transport_close_error, "close")
    else:
        assert hci_result is not None
        required = (hci_result["local_version"], hci_result["buffer_size"], hci_result["bd_addr"])
        result["status"] = "complete" if all(value is not None for value in required) else "incomplete"
    if run_state.close_errors:
        result["cleanup_errors"] = list(run_state.close_errors)
        if "close_error" not in result:
            result["close_error"] = run_state.close_errors[0]
    if original_error is not None and transport_close_error is not None:
        result["close_error"] = exception_details(transport_close_error, "close")
    return result


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="open only the selected unique adapter and run bounded standard-HCI initialization",
    )
    parser.add_argument('--target', type=Path, help='required exact transmitter JSON selection; no default adapter')
    args = parser.parse_args(argv)
    try:
        target = load_usb_target(args.target) if args.target else None
        if target is None:
            raise ProbeError('explicit transmitter selection is required')
    except (OSError, ValueError, KeyError, TypeError, ProbeError) as error:
        _emit({'mode': 'preflight', 'status': 'invalid_target', 'error': exception_details(error, 'select'),
               'device_opened': False, 'hci_command_sent': False})
        return 2

    # Suppress Bumble/USB debug records so key/passkey material cannot reach the CLI log.
    logging.getLogger("bumble").setLevel(logging.CRITICAL)
    logging.getLogger("usb1").setLevel(logging.CRITICAL)

    if not args.initialize:
        try:
            result = enumerate_usb_descriptors(target=target)
        except Exception as error:
            _emit(
                {
                    "mode": "preflight",
                    "status": "unavailable",
                    "error": exception_details(error, "open"),
                    "device_opened": False,
                    "hci_command_sent": False,
                    "bumble_commit": BUMBLE_PINNED_COMMIT,
                }
            )
            return 2
        result["status"] = (
            "candidate" if result["candidate_count"] == 1 and result["candidates"][0].get("standard_hci_endpoints")
                and result["candidates"][0].get("recommended_backend", 'winusb') else
            "not_found" if result["candidate_count"] == 0 else
            "ambiguous_or_incompatible"
        )
        # A descriptor match is only a candidate, never proof of working ACL/audio
        # transport and never sufficient to approve a paid conversion.
        result['payment_eligible'] = False
        result['qualification'] = 'needs_hci_and_link_test' if result['status'] == 'candidate' else 'blocked_preflight'
        candidate = result['candidates'][0] if result['candidate_count'] == 1 else {}
        result['recommended_backend'] = candidate.get('recommended_backend', 'winusb' if result['status'] == 'candidate' else '')
        result['profile'] = candidate.get('profile', '')
        _emit(result)
        return 0 if result["status"] == "candidate" else 2

    run_state = ProbeRunState()
    result = asyncio.run(
        initialize_with_transport(
            lambda state: open_selected_bumble_transport(run_state=state, target=target),
            lambda transport, state: _make_bumble_device(transport, state),
            run_state=run_state,
            target=target,
        )
    )
    _emit(result)
    return 0 if result["status"] == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())
