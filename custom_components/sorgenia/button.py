"""Diagnostic button: renew the Sorgenia token on demand."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SorgeniaRefreshTokenButton(coordinator, entry.entry_id)])


class SorgeniaRefreshTokenButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Refresh token now"
    _attr_icon = "mdi:key-refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_refresh_token_now"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=getattr(coordinator.entry, "title", None) or "Sorgenia",
            manufacturer="Sorgenia (unofficial)",
            model="Customer area",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        """Try a token renewal now; the result is shown by the Token expiry sensor."""
        try:
            await self.coordinator.async_refresh_access_token()
        except ConfigEntryAuthFailed:
            _LOGGER.warning("Manual Sorgenia token refresh rejected: %s", self.coordinator.token_error)
        except UpdateFailed as err:
            _LOGGER.warning("Manual Sorgenia token refresh failed: %s", err)
        self.coordinator.async_update_listeners()
