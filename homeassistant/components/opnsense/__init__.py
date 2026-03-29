"""Support for OPNsense Routers."""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import logging
from typing import Any, cast
from urllib.parse import urlparse

import aiohttp
from aiopnsense import OPNsenseClient
import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    discovery,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_API_SECRET,
    CONF_DEVICE_UNIQUE_ID,
    CONF_INTERFACE_CLIENT,
    CONF_TRACKER_INTERFACES,
    DOMAIN,
    INTEGRATION_TITLE,
    OPNSENSE_DATA,
    TRACKED_MACS,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_URL): cv.url,
                vol.Required(CONF_API_KEY): cv.string,
                vol.Required(CONF_API_SECRET): cv.string,
                vol.Optional(CONF_VERIFY_SSL, default=False): cv.boolean,
                vol.Optional(CONF_TRACKER_INTERFACES, default=[]): vol.All(
                    cv.ensure_list, [cv.string]
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _entry_storage(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return integration storage mapping keyed by config entry id."""
    return cast(dict[str, dict[str, Any]], hass.data.setdefault(OPNSENSE_DATA, {}))


def _is_private_ip(url: str) -> bool:
    """Return True when URL host is a private IP address."""
    host = urlparse(url).hostname
    if not host:
        return False

    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _extract_arp_value(entry: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """Extract first available ARP value from candidate keys."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class OPNsenseClientAdapter:
    """aiopnsense adapter that preserves legacy tracker payload shape."""

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        api_key: str,
        api_secret: str,
        verify_ssl: bool,
    ) -> None:
        """Initialize adapter."""
        self._client = OPNsenseClient(
            url=url,
            username=api_key,
            password=api_secret,
            session=async_create_clientsession(
                hass=hass,
                raise_for_status=False,
                cookie_jar=aiohttp.CookieJar(unsafe=_is_private_ip(url)),
            ),
            opts={"verify_ssl": verify_ssl},
        )

    async def async_get_arp(self) -> list[dict[str, Any]]:
        """Return ARP table entries in legacy field format."""
        raw_arp = await self._client.get_arp_table(resolve_hostnames=True)
        if not isinstance(raw_arp, list):
            return []

        normalized: list[dict[str, Any]] = []
        for entry in raw_arp:
            if not isinstance(entry, Mapping):
                continue

            normalized.append(
                {
                    "hostname": _extract_arp_value(entry, ("hostname", "host")),
                    "intf": _extract_arp_value(entry, ("intf", "interface", "if")),
                    "intf_description": _extract_arp_value(
                        entry,
                        (
                            "intf_description",
                            "interface_name",
                            "interface_description",
                            "if_descr",
                        ),
                    ),
                    "ip": _extract_arp_value(entry, ("ip", "address")),
                    "mac": _extract_arp_value(entry, ("mac",)),
                    "manufacturer": _extract_arp_value(
                        entry, ("manufacturer", "vendor")
                    ),
                }
            )

        return normalized

    async def async_get_interfaces(self) -> list[str]:
        """Return interface display names for legacy tracker filtering."""
        raw_interfaces = await self._client.get_interfaces()
        if not isinstance(raw_interfaces, Mapping):
            return []

        interfaces: list[str] = []
        for interface in raw_interfaces.values():
            if isinstance(interface, str) and interface:
                interfaces.append(interface)
                continue

            if not isinstance(interface, Mapping):
                continue

            for key in (
                "name",
                "description",
                "if",
                "interface",
                "intf_description",
            ):
                value = interface.get(key)
                if isinstance(value, str) and value:
                    interfaces.append(value)
                    break

        return list(dict.fromkeys(interfaces))

    async def async_get_device_unique_id(self) -> str | None:
        """Return device unique id when available."""
        device_id = await self._client.get_device_unique_id()
        if not isinstance(device_id, str) or not device_id:
            return None
        return device_id

    async def async_close(self) -> None:
        """Close underlying client resources."""
        await self._client.async_close()


async def _async_import_from_yaml(
    hass: HomeAssistant, yaml_config: Mapping[str, Any]
) -> None:
    """Import YAML config into a config entry and raise a deprecation issue."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_URL: yaml_config[CONF_URL],
            CONF_API_KEY: yaml_config[CONF_API_KEY],
            CONF_API_SECRET: yaml_config[CONF_API_SECRET],
            CONF_VERIFY_SSL: yaml_config.get(CONF_VERIFY_SSL, False),
            CONF_TRACKER_INTERFACES: list(yaml_config.get(CONF_TRACKER_INTERFACES, [])),
        },
    )

    if (
        result.get("type") == FlowResultType.ABORT
        and result.get("reason") != "already_configured"
    ):
        _LOGGER.warning("Failed to import OPNsense YAML config: %s", result)
        return

    ir.async_create_issue(
        hass,
        HOMEASSISTANT_DOMAIN,
        f"deprecated_yaml_{DOMAIN}",
        breaks_in_ha_version="2027.1.0",
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
        translation_placeholders={
            "domain": DOMAIN,
            "integration_title": INTEGRATION_TITLE,
        },
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the OPNsense integration."""
    if DOMAIN in config:
        hass.async_create_task(_async_import_from_yaml(hass, config[DOMAIN]))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OPNsense from a config entry."""
    data = entry.data

    url = data[CONF_URL]
    api_key = data[CONF_API_KEY]
    api_secret = data[CONF_API_SECRET]
    verify_ssl = data[CONF_VERIFY_SSL]
    tracker_interfaces = data.get(CONF_TRACKER_INTERFACES, [])

    client = OPNsenseClientAdapter(
        hass=hass,
        url=url,
        api_key=api_key,
        api_secret=api_secret,
        verify_ssl=verify_ssl,
    )

    try:
        arp_entries = await client.async_get_arp()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
        raise ConfigEntryNotReady(
            "Failure while connecting to OPNsense API endpoint"
        ) from err

    if tracker_interfaces:
        try:
            interfaces = await client.async_get_interfaces()
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
            raise ConfigEntryNotReady(
                "Failure while validating OPNsense tracker interfaces"
            ) from err

        for interface in tracker_interfaces:
            if interface not in interfaces:
                raise ConfigEntryNotReady(
                    f"Specified OPNsense tracker interface {interface} is not found"
                )

    migrated_data = dict(entry.data)
    tracked_macs: list[str] = []
    for arp_entry in arp_entries:
        mac = arp_entry.get("mac")
        if isinstance(mac, str):
            normalized = mac.lower()
            if normalized and normalized not in tracked_macs:
                tracked_macs.append(normalized)

    device_unique_id = await client.async_get_device_unique_id()
    if (
        device_unique_id
        and migrated_data.get(CONF_DEVICE_UNIQUE_ID) != device_unique_id
    ):
        migrated_data[CONF_DEVICE_UNIQUE_ID] = device_unique_id
    if tracked_macs:
        migrated_data[TRACKED_MACS] = tracked_macs
    if migrated_data != entry.data or entry.unique_id != device_unique_id:
        hass.config_entries.async_update_entry(
            entry,
            data=migrated_data,
            unique_id=device_unique_id or entry.unique_id,
        )

    entry.runtime_data = {
        CONF_INTERFACE_CLIENT: client,
        CONF_TRACKER_INTERFACES: list(tracker_interfaces),
    }
    _entry_storage(hass)[entry.entry_id] = entry.runtime_data

    await discovery.async_load_platform(
        hass,
        Platform.DEVICE_TRACKER,
        DOMAIN,
        {"entry_id": entry.entry_id},
        hass.config.as_dict(),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an OPNsense config entry."""
    runtime_data = cast(dict[str, Any], getattr(entry, "runtime_data", {}))
    client = runtime_data.get(CONF_INTERFACE_CLIENT)
    if isinstance(client, OPNsenseClientAdapter):
        await client.async_close()

    if OPNSENSE_DATA in hass.data:
        hass.data[OPNSENSE_DATA].pop(entry.entry_id, None)
    return True
