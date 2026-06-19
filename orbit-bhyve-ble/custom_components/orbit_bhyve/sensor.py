"""Sensor platform — battery, signal, timing, and water volume sensors."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    UnitOfTime,
    UnitOfVolume,
)
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
    entities: list[SensorEntity] = []
    for coord in runtime.coordinators.values():
        device = coord.device
        if isinstance(device, BHyveHubDevice):
            continue
        if device.battery_pct is not None:
            entities.append(BHyveBatterySensor(coord))
        if device.battery_mv is not None:
            entities.append(BHyveBatteryVoltageSensor(coord))
        entities.append(BHyveTimeRemainingSensor(coord))
        entities.append(BHyveLastWateringSensor(coord))
        entities.append(BHyveRssiSensor(coord))
        entities.append(BHyveWaterVolumeSensor(coord))
    async_add_entities(entities)


class _BHyveDeviceSensorBase(CoordinatorEntity[BHyveDeviceCoordinator], SensorEntity):
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


class BHyveBatterySensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_battery"
        self._attr_name = "Battery"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.device.battery_pct


class BHyveBatteryVoltageSensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.unique_id}_battery_mv"
        self._attr_name = "Battery voltage"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.device.battery_mv


class BHyveTimeRemainingSensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_time_remaining"
        self._attr_name = "Time remaining"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data or self.coordinator.device.state
        if not state.is_watering:
            return None
        return state.seconds_remaining


class BHyveLastWateringSensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_last_watering"
        self._attr_name = "Last watering"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.last_command_at


class BHyveRssiSensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_rssi"
        self._attr_name = "Signal strength"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.device.rssi


class BHyveWaterVolumeSensor(_BHyveDeviceSensorBase):
    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: BHyveDeviceCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.unique_id}_water_volume"
        self._attr_name = "Water volume"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data or self.coordinator.device.state
        return state.water_volume_gal
