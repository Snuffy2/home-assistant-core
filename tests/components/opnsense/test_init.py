"""Unit tests for the integration package initialization and lifecycle helpers."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.core import HomeAssistant
import homeassistant.helpers.aiohttp_client as _hc

# import the package module object so we can access its functions/attrs
init_mod = importlib.import_module("homeassistant.components.opnsense")


@pytest.fixture
def ignore_missing_translations(request):
    """Ignore repair issue translation checks for the specific setup tests that raise them."""
    ignored = {
        "test_async_setup_entry_device_id_mismatch": [
            "component.opnsense.issues.device_id_mismatched.title",
            "component.opnsense.issues.device_id_mismatched.description",
        ],
        "test_async_setup_entry_firmware_below_min": [
            "component.opnsense.issues.below_min_firmware.title",
            "component.opnsense.issues.below_min_firmware.description",
        ],
    }
    return ignored.get(getattr(request.node, "originalname", request.node.name), [])


@pytest.fixture(autouse=True)
def _patch_hass_async_create_clientsession(monkeypatch):
    """Autouse fixture to stub Home Assistant's async_create_clientsession.

    Some tests use a minimal `hass` object (SimpleNamespace) which does not
    provide the full helper; patch the helper to return a lightweight
    session-like object to avoid opening real network resources.
    """

    def _fake_create_clientsession(*args, **kwargs):
        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                await self.close()
                return False

            async def close(self):
                return True

        return _FakeSession()

    # Patch both the imported module object and the import-path string so
    # tests are resilient in different environments. Use raising=False so
    # missing targets don't cause the fixture to fail.
    monkeypatch.setattr(
        _hc, "async_create_clientsession", _fake_create_clientsession, raising=False
    )
    monkeypatch.setattr(
        "homeassistant.helpers.aiohttp_client.async_create_clientsession",
        _fake_create_clientsession,
        raising=False,
    )

    # Also patch the integration's local import of the helper so the
    # integration doesn't create a real session when tests import the
    # symbol into its own namespace (e.g., `from ...aiohttp_client import async_create_clientsession`).
    # Use raising=False and a fallback import path to be resilient.
    monkeypatch.setattr(
        init_mod,
        "async_create_clientsession",
        _fake_create_clientsession,
        raising=False,
    )


@pytest.mark.asyncio
async def test_async_setup_entry_success(
    monkeypatch,
    ph_hass,
    coordinator_capture,
    fake_client,
    fake_coordinator,
    make_config_entry,
):
    """async_setup_entry should succeed with valid client and coordinator."""
    monkeypatch.setattr(init_mod, "OPNsenseClient", fake_client())
    # use shared coordinator capture fixture
    monkeypatch.setattr(
        init_mod,
        "OPNsenseDataUpdateCoordinator",
        coordinator_capture.factory(fake_coordinator),
    )

    # create a minimal config entry using the shared helper so all fields
    # (data, options, title, entry_id, unique_id, listeners) are set
    entry = make_config_entry(
        data={
            init_mod.CONF_URL: "http://1.2.3.4",
            init_mod.CONF_USERNAME: "u",
            init_mod.CONF_PASSWORD: "p",
            init_mod.CONF_DEVICE_UNIQUE_ID: "dev1",
        },
        options={},
    )

    # use migration fixture which may wrap the real hass or provide a MagicMock
    hass = ph_hass
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()

    # ensure hass.data is a real dict for the integration to populate
    hass.data = {}

    res = await init_mod.async_setup_entry(hass, entry)
    assert res is True
    assert init_mod.DOMAIN in hass.data and entry.entry_id in hass.data[init_mod.DOMAIN]


@pytest.mark.asyncio
async def test_async_setup_entry_device_id_mismatch(
    monkeypatch,
    ph_hass,
    coordinator_capture,
    fake_client,
    fake_coordinator,
    make_config_entry,
):
    """async_setup_entry should fail when client reports mismatched device id."""
    monkeypatch.setattr(init_mod, "OPNsenseClient", fake_client(device_id="other"))
    # use shared coordinator capture fixture
    monkeypatch.setattr(
        init_mod,
        "OPNsenseDataUpdateCoordinator",
        coordinator_capture.factory(fake_coordinator),
    )

    # use the shared helper to construct the entry for consistency
    entry = make_config_entry(
        data={
            init_mod.CONF_URL: "http://1.2.3.4",
            init_mod.CONF_USERNAME: "u",
            init_mod.CONF_PASSWORD: "p",
            init_mod.CONF_DEVICE_UNIQUE_ID: "dev1",
        },
        options={},
    )

    hass = ph_hass
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()

    # should return False because router id mismatches and coordinator.shutdown called
    res = await init_mod.async_setup_entry(hass, entry)
    assert res is False

    # ensure coordinator shutdown was invoked
    assert any(getattr(inst, "shut", False) for inst in coordinator_capture.instances)


@pytest.mark.asyncio
async def test_async_update_listener_not_reload(monkeypatch, make_config_entry):
    """_async_update_listener should set SHOULD_RELOAD True and not call reload when flag False."""
    entry = make_config_entry(entry_id="e", unique_id="u")
    # ensure runtime_data exists and set SHOULD_RELOAD to False
    setattr(entry.runtime_data, init_mod.SHOULD_RELOAD, False)

    # hass with config_entries.async_reload not called
    hass = MagicMock(spec=HomeAssistant)
    hass.config_entries = MagicMock()
    hass.config_entries.async_reload = AsyncMock()

    # should set SHOULD_RELOAD back to True and not call reload
    await init_mod._async_update_listener(hass, entry)
    assert getattr(entry.runtime_data, init_mod.SHOULD_RELOAD) is True
    hass.config_entries.async_reload.assert_not_called()


@pytest.mark.asyncio
async def test_async_remove_config_entry_device_branches(monkeypatch, hass):
    """Verify removal logic for config entry device registry branches."""
    device = MagicMock()
    device.via_device_id = True
    device.id = "d1"
    res = await init_mod.async_remove_config_entry_device(hass, None, device)
    assert res is False

    # device_entry with linked entity -> False
    device = MagicMock()
    device.via_device_id = False
    device.id = "d2"

    class ER:
        pass

    # fake registry that returns one entity with matching device_id
    ent = MagicMock()
    ent.device_id = "d2"
    monkeypatch.setattr(init_mod.er, "async_get", lambda hass: ER())
    monkeypatch.setattr(
        init_mod.er,
        "async_entries_for_config_entry",
        lambda registry, config_entry_id: [ent],
    )
    res = await init_mod.async_remove_config_entry_device(
        hass, MagicMock(entry_id="x"), device
    )
    assert res is False


@pytest.mark.asyncio
async def test_async_remove_config_entry_device_no_linked_entities(monkeypatch):
    """When no linked entities exist for a device, removal should succeed (return True)."""
    # device not linked via via_device_id and has an id
    device = MagicMock()
    device.via_device_id = False
    device.id = "d3"

    # fake entity registry returns no entities for the config entry
    ER = MagicMock()
    monkeypatch.setattr(init_mod.er, "async_get", lambda hass: ER)
    monkeypatch.setattr(
        init_mod.er,
        "async_entries_for_config_entry",
        lambda registry, config_entry_id: [],
    )

    # call the removal helper with a dummy config entry
    res = await init_mod.async_remove_config_entry_device(
        None, MagicMock(entry_id="x"), device
    )
    assert res is True


@pytest.mark.asyncio
async def test_async_unload_entry_and_pop(ph_hass, make_config_entry):
    """async_unload_entry removes entry from hass.data and closes the client."""
    entry = make_config_entry(entry_id="e_unload")
    entry.as_dict = lambda: {"id": "x"}
    # use the constant names used by the integration
    setattr(entry.runtime_data, init_mod.LOADED_PLATFORMS, ["p1"])
    fake_client = MagicMock()
    fake_client.async_close = AsyncMock()
    setattr(entry.runtime_data, init_mod.OPNSENSE_CLIENT, fake_client)

    hass = ph_hass
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.data = {init_mod.DOMAIN: {entry.entry_id: fake_client}}
    res = await init_mod.async_unload_entry(hass, entry)
    assert res is True
    assert entry.entry_id not in hass.data[init_mod.DOMAIN]
    fake_client.async_close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("should_raise", [False, True])
async def test_async_setup_calls_services_and_handles_exceptions(
    monkeypatch, ph_hass, should_raise
):
    """async_setup should call async_setup_services; exceptions should propagate."""
    if should_raise:
        mock_services = AsyncMock(side_effect=RuntimeError("fail"))
    else:
        mock_services = AsyncMock(return_value=None)

    monkeypatch.setattr(init_mod, "async_setup_services", mock_services)

    if should_raise:
        with pytest.raises(RuntimeError):
            await init_mod.async_setup(ph_hass, {})
        mock_services.assert_awaited_once()
    else:
        res = await init_mod.async_setup(ph_hass, {})
        assert res is True
        mock_services.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_setup_imports_legacy_yaml_with_reduced_defaults(
    monkeypatch, ph_hass
):
    """Legacy YAML import should create a config flow with telemetry-only defaults."""
    mock_services = AsyncMock(return_value=None)
    async_init = AsyncMock(return_value={"type": "create_entry"})
    created_tasks: list[asyncio.Task] = []

    def _capture_task(coro):
        task = asyncio.create_task(coro)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(init_mod, "async_setup_services", mock_services)
    ph_hass.config_entries.flow.async_init = async_init
    ph_hass.async_create_task = MagicMock(side_effect=_capture_task)

    result = await init_mod.async_setup(
        ph_hass,
        {
            init_mod.DOMAIN: {
                init_mod.CONF_URL: "https://router.example",
                init_mod.CONF_API_KEY: "api-key",
                init_mod.CONF_API_SECRET: "api-secret",
                init_mod.CONF_VERIFY_SSL: True,
                init_mod.CONF_TRACKER_INTERFACES: ["lan"],
            }
        },
    )

    assert result is True
    mock_services.assert_awaited_once()
    assert len(created_tasks) == 1
    await created_tasks[0]
    async_init.assert_awaited_once()
    flow_data = async_init.await_args.kwargs["data"]
    assert flow_data[init_mod.CONF_USERNAME] == "api-key"
    assert flow_data[init_mod.CONF_PASSWORD] == "api-secret"
    assert flow_data[init_mod.CONF_GRANULAR_SYNC_OPTIONS] is True
    assert flow_data[init_mod.CONF_SYNC_TELEMETRY] is True
    assert flow_data[init_mod.CONF_SYNC_INTERFACES] is False
    assert flow_data["_import_options"][init_mod.CONF_DEVICE_TRACKER_ENABLED] is True
    assert flow_data["_import_options"][init_mod.CONF_DEVICES] == []


@pytest.mark.asyncio
async def test_async_update_listener_reload_and_remove(
    monkeypatch, ph_hass, make_config_entry
):
    """When SHOULD_RELOAD True and sync disabled, update listener schedules reload and removes entities."""
    # Prepare entry with SHOULD_RELOAD True and granular sync option disabled to force removal_prefixes
    entry = make_config_entry(
        data={
            init_mod.CONF_DEVICE_UNIQUE_ID: "dev1",
            "sync_telemetry": False,
        },
        unique_id="u123",
    )
    setattr(entry.runtime_data, init_mod.SHOULD_RELOAD, True)
    # config entries and hass async reload stub
    # use migration fixture which provides config_entries and async helpers
    hass = ph_hass
    hass.config_entries.async_reload = AsyncMock()
    hass.data = {}

    # construct an entity that should be removed by unique_id prefix
    class Ent:
        def __init__(self, entity_id, unique_id):
            self.entity_id = entity_id
            self.unique_id = unique_id

    # explicitly use the 'sync_telemetry' prefix so the test targets the intended sync item
    prefix = list(init_mod.GRANULAR_SYNC_PREFIX["sync_telemetry"])
    pre = prefix[0]
    ent = Ent("sensor.x", f"{entry.unique_id}_{pre}_suffix")

    # monkeypatch entity registry functions
    ER = MagicMock()
    ER.async_remove = MagicMock()
    monkeypatch.setattr(init_mod.er, "async_get", lambda hass: ER)
    monkeypatch.setattr(
        init_mod.er,
        "async_entries_for_config_entry",
        lambda registry, config_entry_id: [ent],
    )
    # patch device registry to return no devices and provide async_remove_device
    DR = MagicMock()
    DR.async_remove_device = MagicMock()
    monkeypatch.setattr(init_mod.dr, "async_get", lambda hass: DR)
    monkeypatch.setattr(
        init_mod.dr,
        "async_entries_for_config_entry",
        lambda registry, config_entry_id: [],
    )

    # config option already provided via factory; no mutation needed

    # Ensure hass.async_create_task exists (ph_hass MagicMock fallback may not
    # provide it). Tests expect this to exist so they can assert it was called.
    if not hasattr(hass, "async_create_task"):
        hass.async_create_task = MagicMock()

    await init_mod._async_update_listener(hass, entry)

    # async_create_task should have been used to schedule reload
    assert hass.async_create_task.called

    # entity matched by prefix should be removed; no devices to remove
    ER.async_remove.assert_called_once_with(ent.entity_id)
    DR.async_remove_device.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dt_enabled", "via_device_id", "expect_removed"),
    [
        (False, True, True),
        (False, False, False),
        (True, True, False),
    ],
)
async def test_async_update_listener_device_removal_param(
    monkeypatch, ph_hass, make_config_entry, dt_enabled, via_device_id, expect_removed
):
    """Parameterized: ensure devices are removed only when device tracker disabled and via_device_id is True."""
    # create an entry with the device tracker option set per parameter
    entry = make_config_entry(
        data={init_mod.CONF_DEVICE_UNIQUE_ID: "dev1"},
        options={init_mod.CONF_DEVICE_TRACKER_ENABLED: dt_enabled},
    )
    setattr(entry.runtime_data, init_mod.SHOULD_RELOAD, True)

    hass = ph_hass
    hass.config_entries.async_reload = AsyncMock()
    hass.data = {}

    # ensure hass.async_create_task exists for scheduling reload
    if not hasattr(hass, "async_create_task"):
        hass.async_create_task = MagicMock()

    # prepare a single device entry returned by the device registry
    device = MagicMock()
    device.via_device_id = via_device_id
    device.id = "d_device"
    device.name = "devname"

    DR = MagicMock()
    DR.async_remove_device = MagicMock()
    monkeypatch.setattr(init_mod.dr, "async_get", lambda hass: DR)
    monkeypatch.setattr(
        init_mod.dr,
        "async_entries_for_config_entry",
        lambda registry, config_entry_id: [device],
    )

    # ensure no entity registry removals interfere
    monkeypatch.setattr(init_mod.er, "async_get", lambda hass: MagicMock())
    monkeypatch.setattr(
        init_mod.er,
        "async_entries_for_config_entry",
        lambda registry, config_entry_id: [],
    )

    await init_mod._async_update_listener(hass, entry)

    if expect_removed:
        DR.async_remove_device.assert_called_once_with(device.id)
    else:
        DR.async_remove_device.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_firmware_below_min(
    monkeypatch,
    ph_hass,
    coordinator_capture,
    fake_client,
    fake_coordinator,
    make_config_entry,
):
    """async_setup_entry returns False for devices with firmware below minimum supported."""
    # fake client where device id matches but firmware is below min
    monkeypatch.setattr(init_mod, "OPNsenseClient", fake_client(firmware_version="1.0"))
    monkeypatch.setattr(
        init_mod,
        "OPNsenseDataUpdateCoordinator",
        coordinator_capture.factory(fake_coordinator),
    )

    entry = make_config_entry(
        data={
            init_mod.CONF_URL: "http://1.2.3.4",
            init_mod.CONF_USERNAME: "u",
            init_mod.CONF_PASSWORD: "p",
            init_mod.CONF_DEVICE_UNIQUE_ID: "dev1",
        }
    )
    # use hass fixture for aiohttp helpers
    hass = ph_hass
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()
    hass.data.setdefault("aiohttp_connector", {})

    res = await init_mod.async_setup_entry(hass, entry)
    assert res is False


@pytest.mark.asyncio
async def test_async_setup_entry_awesomeversion_exception(
    monkeypatch,
    ph_hass,
    coordinator_capture,
    fake_client,
    fake_coordinator,
    make_config_entry,
):
    """async_setup_entry should continue when AwesomeVersion comparison raises an exception."""

    # fake client where device id matches but awesomeversion comparison raises
    # monkeypatch AwesomeVersion to a class that raises on comparison
    class DummyAV:
        def __init__(self, v):
            self.v = v

        def __lt__(self, other):
            raise init_mod.awesomeversion.exceptions.AwesomeVersionCompareException

    monkeypatch.setattr(init_mod, "OPNsenseClient", fake_client())
    monkeypatch.setattr(
        init_mod,
        "OPNsenseDataUpdateCoordinator",
        coordinator_capture.factory(fake_coordinator),
    )
    monkeypatch.setattr(init_mod.awesomeversion, "AwesomeVersion", DummyAV)
    entry = make_config_entry(
        data={
            init_mod.CONF_URL: "http://1.2.3.4",
            init_mod.CONF_USERNAME: "u",
            init_mod.CONF_PASSWORD: "p",
            init_mod.CONF_DEVICE_UNIQUE_ID: "dev1",
        }
    )
    hass = ph_hass
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()

    res = await init_mod.async_setup_entry(hass, entry)
    assert res is True


@pytest.mark.asyncio
async def test_async_unload_entry_unload_fails(ph_hass, make_config_entry):
    """async_unload_entry returns False and retains hass.data when platform unload fails."""
    entry = make_config_entry(entry_id="e_unload_fail")
    entry.as_dict = lambda: {"id": "x"}
    setattr(entry.runtime_data, init_mod.LOADED_PLATFORMS, ["p1"])
    fake_client = MagicMock()
    fake_client.async_close = AsyncMock()
    setattr(entry.runtime_data, init_mod.OPNSENSE_CLIENT, fake_client)

    hass = ph_hass
    # unload_platforms returns False
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    hass.data = {init_mod.DOMAIN: {entry.entry_id: fake_client}}
    res = await init_mod.async_unload_entry(hass, entry)
    assert res is False
    # hass.data should still have the entry
    assert entry.entry_id in hass.data[init_mod.DOMAIN]
    fake_client.async_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_setup_entry_delete_uses_min_issue_id(
    monkeypatch,
    ph_hass,
    coordinator_capture,
    fake_client,
    fake_coordinator,
    make_config_entry,
):
    """async_setup_entry removes the stale below-min repair once firmware is supported."""
    firmware_str = "26.1.1"
    monkeypatch.setattr(
        init_mod, "OPNsenseClient", fake_client(firmware_version=firmware_str)
    )
    monkeypatch.setattr(
        init_mod,
        "OPNsenseDataUpdateCoordinator",
        coordinator_capture.factory(fake_coordinator),
    )

    calls = MagicMock()
    monkeypatch.setattr(init_mod.ir, "async_delete_issue", calls)

    entry = make_config_entry(
        data={
            init_mod.CONF_URL: "http://1.2.3.4",
            init_mod.CONF_USERNAME: "u",
            init_mod.CONF_PASSWORD: "p",
            init_mod.CONF_DEVICE_UNIQUE_ID: "dev1",
        }
    )
    hass = ph_hass
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = MagicMock()
    res = await init_mod.async_setup_entry(hass, entry)
    assert res is True

    # Confirm delete_issue was called for the expected issue id
    expected_min = f"{entry.data[init_mod.CONF_DEVICE_UNIQUE_ID]}_opnsense_below_min_firmware_{init_mod.OPNSENSE_MIN_FIRMWARE}"
    assert calls.called, "async_delete_issue should have been called"
    issue_ids = [call[0][2] for call in calls.call_args_list if len(call[0]) > 2]
    assert expected_min in issue_ids


@pytest.mark.asyncio
async def test_async_setup_entry_with_device_tracker_enabled(
    monkeypatch,
    ph_hass,
    coordinator_capture,
    fake_client,
    fake_coordinator,
    make_config_entry,
):
    """Device tracker option creates a device-tracker coordinator and triggers initial refresh."""
    monkeypatch.setattr(init_mod, "OPNsenseClient", fake_client())
    monkeypatch.setattr(
        init_mod,
        "OPNsenseDataUpdateCoordinator",
        coordinator_capture.factory(fake_coordinator),
    )

    entry = make_config_entry(
        data={
            init_mod.CONF_URL: "http://1.2.3.4",
            init_mod.CONF_USERNAME: "u",
            init_mod.CONF_PASSWORD: "p",
            init_mod.CONF_DEVICE_UNIQUE_ID: "dev1",
        },
        options={init_mod.CONF_DEVICE_TRACKER_ENABLED: True},
    )

    hass = ph_hass
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = MagicMock()
    res = await init_mod.async_setup_entry(hass, entry)
    assert res is True
    # ensure a device-tracker coordinator was created and its initial refresh ran
    assert any(
        getattr(inst, "_is_device_tracker", False)
        for inst in coordinator_capture.instances
    )
    assert any(
        getattr(inst, "refreshed", False) for inst in coordinator_capture.instances
    )
