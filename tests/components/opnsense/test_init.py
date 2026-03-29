"""Tests for OPNsense init and migration."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.components.opnsense import (
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_DEVICE_TRACKER_ENABLED,
    CONF_DEVICE_UNIQUE_ID,
    CONF_DEVICES,
    CONF_GRANULAR_SYNC_OPTIONS,
    CONF_PASSWORD,
    CONF_SYNC_TELEMETRY,
    CONF_TRACKER_INTERFACES,
    CONF_URL,
    CONF_USERNAME,
    DOMAIN,
    TRACKED_MACS,
    __init__ as opnsense_init,
)
from homeassistant.const import CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry


async def test_async_migrate_entry_from_legacy_api_key_schema(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrate legacy API-key entries into the current schema and options."""

    class FakeClient:
        async def get_device_unique_id(self) -> str:
            return "aa:bb:cc:dd:ee:ff"

        async def async_close(self) -> None:
            return None

    monkeypatch.setattr(opnsense_init, "OPNsenseClient", lambda **kwargs: FakeClient())

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Router",
        unique_id="https://router.local",
        data={
            CONF_URL: "https://router.local",
            CONF_API_KEY: "legacy_key",
            CONF_API_SECRET: "legacy_secret",
            CONF_VERIFY_SSL: False,
            CONF_TRACKER_INTERFACES: ["LAN"],
            TRACKED_MACS: ["AA:BB:CC:DD:EE:01", "aa:bb:cc:dd:ee:01"],
        },
        options={},
    )
    object.__setattr__(entry, "version", 1)
    object.__setattr__(entry, "minor_version", 1)
    entry.add_to_hass(hass)

    assert await opnsense_init.async_migrate_entry(hass, entry)

    assert entry.unique_id == "aa:bb:cc:dd:ee:ff"
    assert entry.data[CONF_USERNAME] == "legacy_key"
    assert entry.data[CONF_PASSWORD] == "legacy_secret"
    assert entry.data[CONF_DEVICE_UNIQUE_ID] == "aa:bb:cc:dd:ee:ff"
    assert entry.data[CONF_GRANULAR_SYNC_OPTIONS] is True
    assert entry.data[CONF_SYNC_TELEMETRY] is True
    assert entry.data[TRACKED_MACS] == ["aa:bb:cc:dd:ee:01"]
    assert entry.options[CONF_DEVICE_TRACKER_ENABLED] is True
    assert entry.options[CONF_DEVICES] == ["aa:bb:cc:dd:ee:01"]
    assert entry.minor_version == 2


async def test_async_migrate_entry_minor_bump_only(hass: HomeAssistant) -> None:
    """Minor version migration bumps already-migrated entries."""
    data: dict[str, Any] = {
        CONF_URL: "https://router.local",
        CONF_USERNAME: "key",
        CONF_PASSWORD: "secret",
        CONF_VERIFY_SSL: False,
        CONF_DEVICE_UNIQUE_ID: "aa:bb:cc:dd:ee:ff",
        CONF_GRANULAR_SYNC_OPTIONS: True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Router",
        unique_id="aa:bb:cc:dd:ee:ff",
        data=data,
        options={CONF_DEVICE_TRACKER_ENABLED: True, CONF_DEVICES: []},
    )
    object.__setattr__(entry, "version", 1)
    object.__setattr__(entry, "minor_version", 1)
    entry.add_to_hass(hass)

    assert await opnsense_init.async_migrate_entry(hass, entry)
    assert entry.minor_version == 2
