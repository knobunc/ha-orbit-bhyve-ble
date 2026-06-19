"""Binary sensor platform — BLE connectivity and watering state."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BHyveDeviceCoordinator
from .devices import BHyveHubDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []
    for coord in runtime.coordinators.values():
        if isinstance(coord.device, BHyveHubDevice):
            continue
        entities.append(BHyveConnectedBinarySensor(coord))
        entities.append(BHyveWateringBinarySensor(coord))
    async_add_entities(entities)


class _BHyveBinarySensorBase(CoordinatorEntity[BHyveDeviceCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.cloud_id)},
            "name": device.name,
            "manufacturer": "Orbit Irrigation",
            "model": device.hardware,
            "sw_version": device.firmware,
            "connections": {("mac", device.mac)} if device.mac else set(),
        }


class BHyveConnectedBinarySensor(_BHyveBinarySensorBase):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_connected"
        self._attr_name = "Connected"

    @property
    def is_on(self) -> bool:
        state = self.coordinator.data or self.coordinator.device.state
        return state.is_connected


class BHyveWateringBinarySensor(_BHyveBinarySensorBase):
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_watering"
        self._attr_name = "Watering"

    @property
    def is_on(self) -> bool:
        state = self.coordinator.data or self.coordinator.device.state
        return state.is_watering
