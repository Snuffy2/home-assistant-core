"""Config flow for OPNsense integration."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
import ipaddress
import logging
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiopnsense import OPNsenseClient
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import homeassistant.helpers.config_validation as cv

from .const import CONF_API_SECRET, CONF_TRACKER_INTERFACES, DOMAIN, INTEGRATION_TITLE

_LOGGER = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    """Normalize URL for unique id checks."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path == "/":
        path = ""
    return parsed._replace(netloc=parsed.netloc.lower(), path=path).geturl()


def _title_from_url(url: str) -> str:
    """Create an entry title from URL."""
    parsed = urlparse(url)
    return parsed.hostname or INTEGRATION_TITLE


def _is_private_ip(url: str) -> bool:
    """Return True when URL host is a private IP address."""
    host = urlparse(url).hostname
    if not host:
        return False

    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _normalize_tracker_interfaces(value: Any) -> list[str]:
    """Normalize configured tracker interfaces from string/list input."""
    if isinstance(value, list):
        normalized = [
            item.strip() for item in value if isinstance(item, str) and item.strip()
        ]
        return list(dict.fromkeys(normalized))

    if not isinstance(value, str):
        return []

    normalized = [
        item.strip()
        for item in re.split(r"[,\n]+", value)
        if isinstance(item, str) and item.strip()
    ]
    return list(dict.fromkeys(normalized))


def _tracker_interfaces_default(user_input: Mapping[str, Any] | None) -> str:
    """Build default text value for tracker interfaces field."""
    if not user_input:
        return ""

    value = user_input.get(CONF_TRACKER_INTERFACES, "")
    if isinstance(value, list):
        return ", ".join(item for item in value if isinstance(item, str))
    if isinstance(value, str):
        return value
    return ""


def _build_user_schema(user_input: Mapping[str, Any] | None = None) -> vol.Schema:
    """Build user step schema."""
    if not user_input:
        user_input = {}

    return vol.Schema(
        {
            vol.Required(CONF_URL, default=user_input.get(CONF_URL, "")): cv.url,
            vol.Required(
                CONF_API_KEY, default=user_input.get(CONF_API_KEY, "")
            ): cv.string,
            vol.Required(
                CONF_API_SECRET, default=user_input.get(CONF_API_SECRET, "")
            ): cv.string,
            vol.Optional(
                CONF_VERIFY_SSL,
                default=user_input.get(CONF_VERIFY_SSL, False),
            ): cv.boolean,
            vol.Optional(
                CONF_TRACKER_INTERFACES,
                default=_tracker_interfaces_default(user_input),
            ): cv.string,
        }
    )


def _extract_interface_names(raw_interfaces: Any) -> list[str]:
    """Extract interface display names from aiopnsense payload."""
    if not isinstance(raw_interfaces, Mapping):
        return []

    names: list[str] = []
    for value in raw_interfaces.values():
        if isinstance(value, str) and value:
            names.append(value)
            continue

        if not isinstance(value, Mapping):
            continue

        for key in ("name", "description", "if", "interface", "intf_description"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                names.append(candidate)
                break

    return list(dict.fromkeys(names))


async def _async_validate_input(
    hass: HomeAssistant, user_input: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate credentials and normalize data."""
    data = dict(user_input)
    data[CONF_TRACKER_INTERFACES] = _normalize_tracker_interfaces(
        user_input.get(CONF_TRACKER_INTERFACES)
    )

    client = OPNsenseClient(
        url=data[CONF_URL],
        username=data[CONF_API_KEY],
        password=data[CONF_API_SECRET],
        session=async_create_clientsession(
            hass=hass,
            raise_for_status=False,
            cookie_jar=aiohttp.CookieJar(unsafe=_is_private_ip(data[CONF_URL])),
        ),
        opts={"verify_ssl": data.get(CONF_VERIFY_SSL, False)},
    )

    try:
        await client.get_arp_table(resolve_hostnames=True)

        tracker_interfaces: list[str] = data[CONF_TRACKER_INTERFACES]
        if tracker_interfaces:
            interface_names = _extract_interface_names(await client.get_interfaces())
            for interface in tracker_interfaces:
                if interface not in interface_names:
                    raise InvalidTrackerInterface
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
        _LOGGER.debug("Failed to validate OPNsense credentials", exc_info=err)
        raise CannotConnect from err
    finally:
        await client.async_close()

    return data


class OPNsenseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an OPNsense config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: MutableMapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                data = await _async_validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidTrackerInterface:
                errors["base"] = "invalid_tracker_interface"
            except Exception:  # pragma: no cover - defensive fallback
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(_normalize_url(data[CONF_URL]))
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=_title_from_url(data[CONF_URL]),
                    data=data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_schema(user_input),
            errors=errors,
        )

    async def async_step_import(
        self, user_input: MutableMapping[str, Any]
    ) -> ConfigFlowResult:
        """Import from configuration.yaml."""
        data = dict(user_input)
        data[CONF_TRACKER_INTERFACES] = _normalize_tracker_interfaces(
            user_input.get(CONF_TRACKER_INTERFACES)
        )

        await self.async_set_unique_id(_normalize_url(data[CONF_URL]))
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=_title_from_url(data[CONF_URL]),
            data=data,
        )


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidTrackerInterface(Exception):
    """Error to indicate configured tracker interface is invalid."""
