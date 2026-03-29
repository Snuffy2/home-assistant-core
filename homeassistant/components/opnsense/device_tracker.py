"""Device tracker support for OPNsense routers."""

from __future__ import annotations

from typing import Any, Protocol

from homeassistant.components.device_tracker import DeviceScanner
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONF_INTERFACE_CLIENT,
    CONF_TRACKER_INTERFACES,
    OPNSENSE_DATA,
    TRACKED_MACS,
)


class OPNsenseClientProtocol(Protocol):
    """Protocol for OPNsense client adapter used by the tracker."""

    async def async_get_arp(self) -> list[dict[str, Any]]:
        """Return ARP table."""


async def async_get_scanner(
    hass: HomeAssistant,
    config: ConfigType,
    discovery_info: DiscoveryInfoType | None = None,
) -> DeviceScanner | None:
    """Configure the OPNsense device_tracker."""
    opnsense_data: dict[str, Any] = hass.data[OPNSENSE_DATA]

    if (
        isinstance(discovery_info, dict)
        and isinstance(discovery_info.get("entry_id"), str)
        and discovery_info["entry_id"] in opnsense_data
    ):
        entry_data = opnsense_data[discovery_info["entry_id"]]
    else:
        # Backward compatibility for any legacy test/setup path.
        entry_data = opnsense_data

    return OPNsenseDeviceScanner(
        entry_data[CONF_INTERFACE_CLIENT],
        entry_data[CONF_TRACKER_INTERFACES],
        hass=hass,
        entry_id=discovery_info.get("entry_id")
        if isinstance(discovery_info, dict)
        and isinstance(discovery_info.get("entry_id"), str)
        else None,
    )


class OPNsenseDeviceScanner(DeviceScanner):
    """This class queries a router running OPNsense."""

    def __init__(
        self,
        client: OPNsenseClientProtocol,
        interfaces: list[str],
        hass: HomeAssistant,
        entry_id: str | None,
    ) -> None:
        """Initialize the scanner."""
        self.last_results: dict[str, Any] = {}
        self.client = client
        self.interfaces = interfaces
        self._hass = hass
        self.entry_id = entry_id

    def _get_mac_addrs(
        self, devices: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Create dict with mac address keys from list of devices."""
        out_devices: dict[str, dict[str, Any]] = {}
        for device in devices:
            if not self.interfaces or device.get("intf_description") in self.interfaces:
                mac = device.get("mac")
                if isinstance(mac, str) and mac:
                    out_devices[mac] = device
        return out_devices

    async def async_scan_devices(self) -> list[str]:
        """Scan for new devices and return a list with found device IDs."""
        await self.async_update_info()
        await self._async_persist_tracked_macs()
        return list(self.last_results)

    async def async_get_device_name(self, device: str) -> str | None:
        """Return the name of the given device or None if we don't know."""
        if device not in self.last_results:
            return None
        name = self.last_results[device].get("hostname")
        return name if isinstance(name, str) and name else None

    async def async_update_info(self) -> bool:
        """Ensure the information from the OPNsense router is up to date."""
        devices = await self.client.async_get_arp()
        self.last_results = self._get_mac_addrs(devices)
        return True

    async def async_get_extra_attributes(self, device: str) -> dict[str, str]:
        """Return the extra attrs of the given device."""
        if device not in self.last_results:
            return {}
        mfg = self.last_results[device].get("manufacturer")
        if not isinstance(mfg, str) or not mfg:
            return {}
        return {"manufacturer": mfg}

    async def _async_persist_tracked_macs(self) -> None:
        """Persist tracked MAC metadata for Step 1 migration."""
        if not self.entry_id:
            return

        entry = self._hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return

        tracked_macs = [
            mac.lower() for mac in self.last_results if isinstance(mac, str)
        ]
        existing_macs = entry.data.get(TRACKED_MACS, [])
        if tracked_macs == existing_macs:
            return

        new_data = dict(entry.data)
        new_data[TRACKED_MACS] = tracked_macs
        self._hass.config_entries.async_update_entry(entry, data=new_data)
