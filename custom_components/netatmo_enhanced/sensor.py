"""Read-only Smarther telemetry and the reported override expiry."""
from datetime import UTC, datetime

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfTemperature, PERCENTAGE
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

FIELDS = (
    ("current_temperature", "Temperatura attuale", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    ("target_temperature", "Temperatura impostata", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    ("humidity", "Umidità", SensorDeviceClass.HUMIDITY, PERCENTAGE),
    ("season", "Stagione", None, None),
    ("mode", "Modalità", None, None),
    ("heating_schedule", "Programmazione riscaldamento", None, None),
    ("cooling_schedule", "Programmazione raffrescamento", None, None),
    ("active_schedule", "Programmazione attiva", None, None),
)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    entities = [EnhancedSensor(coordinator, mid, field, name, cls, unit)
                for mid in coordinator.data for field, name, cls, unit in FIELDS]
    entities.extend(OverrideExpirySensor(coordinator, mid) for mid in coordinator.data)
    async_add_entities(entities)

class EnhancedSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    def __init__(self, coordinator, mid, field, name, cls, unit):
        super().__init__(coordinator)
        self.mid, self.field = mid, field
        self._attr_unique_id = f"{mid}_{field}"
        if name is not None:
            self._attr_name = name
        self._attr_device_class = cls
        self._attr_native_unit_of_measurement = unit
        if field in {"current_temperature", "humidity"}:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mid)}, manufacturer="BTicino",
            model="Smarther (BNS)",
            name=f"Netatmo Enhanced {coordinator.data[mid]['room_name']}",
        )

    @property
    def available(self):
        return (super().available and self.mid in self.coordinator.data
                and self.coordinator.data[self.mid]["reachable"] is not False)

    @property
    def native_value(self):
        return self.coordinator.data.get(self.mid, {}).get(self.field)


class OverrideExpirySensor(EnhancedSensor):
    _attr_translation_key = "override_expires_at"

    def __init__(self, coordinator, mid):
        super().__init__(coordinator, mid, "override_expires_at", None, SensorDeviceClass.TIMESTAMP, None)

    @property
    def native_value(self):
        state = self.coordinator.data.get(self.mid, {})
        if state.get("mode") not in {"manual", "max"}:
            return None
        timestamp = state.get("setpoint_end_time")
        if type(timestamp) not in (int, float) or timestamp <= 0:
            return None
        try:
            return datetime.fromtimestamp(timestamp, UTC)
        except (ValueError, OverflowError, OSError):
            return None
