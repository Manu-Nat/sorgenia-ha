"""Greeners membership binary sensors."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

def _device_info(coordinator, entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=getattr(coordinator.entry, "title", None) or "Sorgenia",
        manufacturer="Sorgenia (unofficial)",
        model="Customer area",
        entry_type=DeviceEntryType.SERVICE,
    )



async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GreenersBinarySensor(coordinator, entry.entry_id, field, name)
        for field, name in (
            ("user_registered", "Greeners user registered"),
            ("played_welcome_iw", "Greeners welcome game played"),
        )
    )


class GreenersBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Represent boolean flags returned by the Greeners status endpoint."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:leaf"

    def __init__(self, coordinator, entry_id: str, field: str, name: str) -> None:
        super().__init__(coordinator)
        self._field = field
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_{field}"
        self._attr_device_info = _device_info(coordinator, entry_id)

    @property
    def is_on(self):
        value = self.coordinator.data["greeners"].get(self._field)
        return value if isinstance(value, bool) else None
