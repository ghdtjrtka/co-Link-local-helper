"""Controller-initialization adapter for the pinned Bumble drivers.

Source pin: Bumble ``a679beb4f36681c6f1b6d72b7e825c6f57f9ef83``.
Initialization delegates to the pinned upstream Bumble controller drivers.

This module adds no HCI opcodes, no completion handling, and no transport/SCO
policy. It only classifies a USB identity against the actual upstream
``RTK_USB_PRODUCTS`` / ``INTEL_USB_PRODUCTS`` tables and delegates initialization
to the upstream driver classes. Unknown identities return ``None`` without any
HCI or vendor request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

BUMBLE_PINNED_COMMIT = "a679beb4f36681c6f1b6d72b7e825c6f57f9ef83"
SCHEMA = "audio-codac-controller-init/1"



class ChipsetInitError(RuntimeError):
    """Base error for a controller-initialization adapter failure."""


class ChipsetFirmwareError(ChipsetInitError):
    """Required local firmware/config is missing before any vendor write."""

    def __init__(self, usb_id: str, missing: str, names: list[str]) -> None:
        self.usb_id = usb_id
        self.missing = missing
        self.names = list(names)
        super().__init__(
            f"{usb_id}: missing required {missing} file(s): {', '.join(self.names)}"
        )


def _load_upstream():
    try:
        from bumble_runtime import ensure_bumble_source
        ensure_bumble_source()
        from bumble.drivers import intel, rtk
    except Exception as error:  # pragma: no cover - exercised by broken installs
        raise ChipsetInitError(
            "pinned Bumble driver modules are unavailable"
        ) from error
    return rtk, intel


def _as_int(value: Any) -> int:
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("0x"):
            return int(text, 16)
        return int(text, 16)
    return int(value)


def _usb_id(vendor_id: int, product_id: int) -> str:
    return f"{vendor_id:04X}:{product_id:04X}"


def _known_plan(
    usb_id: str,
    family: str,
    driver_module: str,
    upstream_table: str,
    firmware_search_env: str,
) -> dict[str, Any]:
    realtek = family == "realtek"
    return {
        "schema": SCHEMA,
        "usb_id": usb_id,
        "family": family,
        "driver_module": driver_module,
        "upstream_table": upstream_table,
        "upstream_driver_available": True,
        "route": "upstream_rtk_driver" if realtek else "upstream_intel_driver",
        "init_stage": "driver_candidate",
        "driver_selected": False,
        "firmware_initialized": False,
        "firmware_required": (
            "conditional_on_rtk_hci_rom_match"
            if realtek
            else "conditional_on_intel_hci_state"
        ),
        "firmware_selection": (
            "after_rtk_hci_rom_match" if realtek else "after_intel_version_tlv"
        ),
        "firmware_name_source": "rtk_hci_rom" if realtek else "intel_version_tlv",
        "firmware_expected_names": None,
        "firmware_search_env": firmware_search_env,
        "vendor_commands": "upstream_only",
        "candidate_for_loader_selection": True,
        "actual_chip_identified": False,
        "qualified_hardware": False,
        "generic_hci_allowed": True,
        "notes": [
            "USB table membership selects a loader candidate, not a chip model.",
            "Firmware names are not derived from the USB ID.",
            "No HCI request is sent by describe_controller_init or by an unknown-ID factory call.",
        ],
    }


def _generic_plan(usb_id: str | None, reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "usb_id": usb_id,
        "family": "generic_hci",
        "driver_module": None,
        "upstream_table": None,
        "upstream_driver_available": False,
        "route": "generic_hci",
        "init_stage": "generic_hci",
        "driver_selected": False,
        "firmware_initialized": False,
        "firmware_required": False,
        "firmware_selection": None,
        "firmware_name_source": None,
        "firmware_expected_names": None,
        "firmware_search_env": None,
        "vendor_commands": "none",
        "candidate_for_loader_selection": False,
        "actual_chip_identified": False,
        "qualified_hardware": False,
        "generic_hci_allowed": True,
        "reason": reason,
        "notes": [
            "Standard HCI remains the path.",
            "No vendor HCI is allowed for this classification.",
        ],
    }


def _plan_for_ids(
    vendor_id: int, product_id: int, rtk: Any, intel: Any
) -> dict[str, Any]:
    usb_id = _usb_id(vendor_id, product_id)
    if (vendor_id, product_id) in rtk.RTK_USB_PRODUCTS:
        return _known_plan(
            usb_id,
            "realtek",
            "bumble.drivers.rtk",
            "RTK_USB_PRODUCTS",
            "BUMBLE_RTK_FIRMWARE_DIR",
        )
    if (vendor_id, product_id) in intel.INTEL_USB_PRODUCTS:
        return _known_plan(
            usb_id,
            "intel",
            "bumble.drivers.intel",
            "INTEL_USB_PRODUCTS",
            "BUMBLE_INTEL_FIRMWARE_DIR",
        )
    return _generic_plan(usb_id, "usb_id_not_in_upstream_controller_tables")


def describe_controller_init(vendor_id: int, product_id: int) -> dict[str, Any]:
    """Return a JSON-safe loader-selection plan from the pinned upstream tables."""
    rtk, intel = _load_upstream()
    return _plan_for_ids(_as_int(vendor_id), _as_int(product_id), rtk, intel)


def _set_strategy(host: Any, plan: dict[str, Any]) -> None:
    host.controller_init_strategy = dict(plan)


async def _rtk_driver(host: Any, rtk: Any, plan: dict[str, Any]):
    if not rtk.Driver.check(host):
        plan = dict(plan)
        plan.update(
            route="generic_hci",
            init_stage="generic_hci",
            driver_selected=False,
            upstream_driver_available=False,
            vendor_commands="none",
            reason="rtk_check_rejected",
        )
        _set_strategy(host, plan)
        return None

    try:
        driver_info = await rtk.Driver.driver_info_for_host(host)
    except Exception as error:
        plan = dict(plan)
        plan.update(
            route="error_identity_query",
            init_stage="error",
            driver_selected=False,
            vendor_commands="none",
            reason=type(error).__name__,
        )
        _set_strategy(host, plan)
        raise

    if driver_info is None:
        plan = dict(plan)
        plan.update(
            route="generic_hci",
            init_stage="generic_hci",
            driver_selected=False,
            vendor_commands="none",
            reason="rtk_unknown_rom_or_already_initialized",
        )
        _set_strategy(host, plan)
        return None

    plan = dict(plan)
    plan.update(
        route="upstream_rtk_driver",
        init_stage="firmware_selection",
        driver_selected=True,
        driver_class="bumble.drivers.rtk.Driver",
        firmware_name=driver_info.fw_name,
        config_name=driver_info.config_name or None,
        config_required=bool(driver_info.config_needed),
        firmware_expected_names=[driver_info.fw_name],
    )

    firmware_path = rtk.Driver.find_binary_path(driver_info.fw_name)
    if not firmware_path:
        plan.update(
            route="error_missing_firmware",
            init_stage="error",
            driver_selected=False,
            vendor_commands="none",
            reason="required_firmware_file_not_found",
            firmware_present=False,
            config_present=False,
        )
        _set_strategy(host, plan)
        raise ChipsetFirmwareError(plan["usb_id"], "firmware", [driver_info.fw_name])

    firmware = Path(firmware_path).read_bytes()
    config = None
    config_present = False
    if driver_info.config_name:
        config_path = rtk.Driver.find_binary_path(driver_info.config_name)
        if config_path:
            config = Path(config_path).read_bytes()
            config_present = True
        elif driver_info.config_needed:
            plan.update(
                route="error_missing_config",
                init_stage="error",
                driver_selected=False,
                vendor_commands="none",
                reason="required_config_file_not_found",
                firmware_present=True,
                config_present=False,
            )
            _set_strategy(host, plan)
            raise ChipsetFirmwareError(
                plan["usb_id"], "config", [driver_info.config_name]
            )

    plan.update(
        init_stage="driver_selected_upstream_will_init",
        firmware_present=True,
        config_present=config_present,
    )
    _set_strategy(host, plan)
    return rtk.Driver(host, driver_info, firmware, config)


async def controller_driver_factory(host: Any):
    """Return an upstream driver for Host.reset, or ``None`` for generic HCI.

    The factory never calls ``init_controller`` itself; ``Host.reset`` does that
    after selecting the returned upstream driver.
    """
    rtk, intel = _load_upstream()
    metadata = getattr(host, "hci_metadata", None) or {}
    vendor_id = metadata.get("vendor_id")
    product_id = metadata.get("product_id")
    if vendor_id is None or product_id is None:
        _set_strategy(
            host,
            _generic_plan(None, "hci_metadata_missing_vendor_or_product_id"),
        )
        return None

    try:
        vendor_id = _as_int(vendor_id)
        product_id = _as_int(product_id)
    except (TypeError, ValueError):
        _set_strategy(
            host,
            _generic_plan(None, "hci_metadata_invalid_vendor_or_product_id"),
        )
        return None

    plan = _plan_for_ids(vendor_id, product_id, rtk, intel)
    _set_strategy(host, plan)

    if plan["family"] == "intel":
        driver = await intel.Driver.for_host(host)
        if driver is None:
            plan = dict(plan)
            plan.update(
                route="generic_hci",
                init_stage="generic_hci",
                driver_selected=False,
                upstream_driver_available=False,
                vendor_commands="none",
                reason="upstream_intel_for_host_returned_none",
            )
            _set_strategy(host, plan)
            return None
        plan = dict(plan)
        plan.update(
            init_stage="driver_selected_upstream_will_init",
            driver_selected=True,
            driver_class="bumble.drivers.intel.Driver",
        )
        _set_strategy(host, plan)
        return driver

    if plan["family"] == "realtek":
        return await _rtk_driver(host, rtk, plan)

    _set_strategy(host, plan)
    return None
