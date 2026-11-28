"""Native Smarther climate and temporary overrides."""
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode, HVACAction
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

PRESET_VALUES = {"schedule": "schedule", "away": "away", "frost_guard": "hg"}
DISPLAY_PRESETS = {
    "home": "schedule", "schedule": "schedule", "away": "away", "hg": "frost_guard",
    "manual": "manual", "max": "boost", "off": "off",
}


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data
    async_add_entities(EnhancedClimate(c, mid) for mid in c.data)


class EnhancedClimate(CoordinatorEntity, ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator, mid):
        super().__init__(coordinator)
        self.mid = mid
        self._attr_unique_id = f"{mid}_climate"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, mid)})

    @property
    def values(self):
        return (self.coordinator.data or {}).get(self.mid, {})

    @property
    def available(self):
        return super().available and bool(self.values) and self.values.get("reachable") is not False and self.values.get("season") in {"heating", "cooling"}

    @property
    def native_current_temperature(self):
        return self.values.get("current_temperature")

    @property
    def native_target_temperature(self):
        return self.values.get("target_temperature")

    @property
    def current_humidity(self):
        return self.values.get("humidity")

    @property
    def min_temp(self):
        return 16 if self.values.get("season") == "cooling" else 7

    @property
    def max_temp(self):
        return 30

    @property
    def hvac_mode(self):
        return HVACMode.OFF if self.values.get("mode") == "off" else HVACMode.AUTO

    @property
    def hvac_modes(self):
        modes = [HVACMode.AUTO]
        if self.values.get("season") == "cooling" or self.hvac_mode == HVACMode.OFF:
            modes.append(HVACMode.OFF)
        return modes

    @property
    def supported_features(self):
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        if self.preset_modes:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def preset_modes(self):
        season = self.values.get("season")
        if season == "heating":
            return [*PRESET_VALUES, "boost"]
        return ["schedule", "boost"] if season == "cooling" else []

    @property
    def preset_mode(self):
        return DISPLAY_PRESETS.get(self.values.get("mode")) if self.preset_modes else None

    @property
    def hvac_action(self):
        heating = self.values.get("boiler_status")
        cooling = self.values.get("cooler_status")
        if type(heating) is bool and type(cooling) is bool:
            if heating and cooling:
                return None
            if heating:
                return HVACAction.HEATING
            if cooling:
                return HVACAction.COOLING
            return HVACAction.OFF if self.hvac_mode == HVACMode.OFF else HVACAction.IDLE
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        season = self.values.get("season")
        running = cooling if season == "cooling" else heating
        if running is True:
            return HVACAction.COOLING if season == "cooling" else HVACAction.HEATING
        if running is False:
            return HVACAction.IDLE
        return None

    @property
    def extra_state_attributes(self):
        return {"temperature_control_mode": self.values.get("season"), "netatmo_setpoint_mode": self.values.get("mode"), "selected_schedule": self.values.get("active_schedule")}

    async def async_set_temperature(self, **kwargs):
        if ATTR_TEMPERATURE not in kwargs:
            raise HomeAssistantError("Temperatura mancante")
        await self.coordinator.async_command(self.mid, "temperature", kwargs[ATTR_TEMPERATURE])

    async def async_set_preset_mode(self, preset_mode):
        if preset_mode not in self.preset_modes:
            raise HomeAssistantError("Modalità non supportata in questa versione")
        if preset_mode == "boost":
            await self.async_set_boost(duration=90)
            return
        await self.coordinator.async_command(self.mid, "preset", PRESET_VALUES[preset_mode])

    async def async_set_boost(self, *, duration):
        await self.coordinator.async_command(self.mid, "boost", duration)

    async def async_stop_boost(self):
        await self.coordinator.async_command(self.mid, "stop_boost", None)

    async def async_cycle_boost(self):
        await self.coordinator.async_command(self.mid, "cycle_boost", None)

    async def async_set_manual_temperature(self, *, temperature, duration):
        await self.coordinator.async_command(self.mid, "temperature_duration", (temperature, duration))

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode == HVACMode.AUTO and "schedule" in self.preset_modes:
            await self.async_set_preset_mode("schedule")
        elif hvac_mode == HVACMode.OFF and self.values.get("season") == "cooling":
            await self.coordinator.async_command(self.mid, "power_off", None)
        elif hvac_mode == self.hvac_mode:
            return
        else:
            raise HomeAssistantError("Modalità HVAC non supportata per la stagione attiva")
