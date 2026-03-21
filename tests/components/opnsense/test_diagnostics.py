"""Tests for OPNsense diagnostics helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.components.opnsense.const import (
    COORDINATOR,
    DEVICE_TRACKER_COORDINATOR,
)
from homeassistant.components.opnsense.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)


async def test_async_get_config_entry_diagnostics_redacts_sensitive_data(
    ph_hass, make_config_entry
) -> None:
    """Diagnostics should include coordinator data while redacting credentials."""
    entry = make_config_entry(
        data={
            "url": "https://router.example",
            "username": "api-user",
            "password": "api-secret",
            "device_unique_id": "router-mac",
        }
    )
    entry.as_dict = lambda: {
        "data": dict(entry.data),
        "options": {},
        "entry_id": entry.entry_id,
    }
    entry.runtime_data = SimpleNamespace(
        **{
            COORDINATOR: MagicMock(data={"system_info": {"name": "Router"}}),
            DEVICE_TRACKER_COORDINATOR: MagicMock(
                data={"arp_table": [{"mac": "aa:bb"}]}
            ),
        }
    )

    diagnostics = await async_get_config_entry_diagnostics(ph_hass, entry)

    assert diagnostics["entry"]["data"]["url"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["username"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["password"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["device_unique_id"] == "**REDACTED**"
    assert diagnostics["coordinator"]["system_info"]["name"] == "Router"
    assert diagnostics["device_tracker_coordinator"]["arp_table"] == [{"mac": "aa:bb"}]


async def test_async_get_device_diagnostics_redacts_device_dict(
    ph_hass, make_config_entry
) -> None:
    """Device diagnostics should include a redacted device payload."""
    entry = make_config_entry(
        data={"url": "https://router.example", "device_unique_id": "router"}
    )
    entry.as_dict = lambda: {"data": dict(entry.data), "entry_id": entry.entry_id}
    entry.runtime_data = SimpleNamespace(
        **{
            COORDINATOR: MagicMock(data={"system_info": {"name": "Router"}}),
        }
    )
    device = MagicMock(
        dict_repr={
            "name": "Tracked Device",
            "configuration_url": "https://router.example/device",
            "device_unique_id": "device-id",
        }
    )

    diagnostics = await async_get_device_diagnostics(ph_hass, entry, device)

    assert diagnostics["device"]["name"] == "Tracked Device"
    assert diagnostics["device"]["configuration_url"] == "https://router.example/device"
    assert diagnostics["device"]["device_unique_id"] == "**REDACTED**"
