"""Native Smarther climate, conservative command surface."""
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode, HVACAction
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data
    async_add_entities(EnhancedClimate(c, mid) for mid in c.data)

class EnhancedClimate(CoordinatorEntity, ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
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
    def current_temperature(self):
        return self.values.get("current_temperature")

    @property
    def target_temperature(self):
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
        # Season selection and power-off commands are not enabled in this pilot.
        return [HVACMode.AUTO, HVACMode.OFF] if self.hvac_mode == HVACMode.OFF else [HVACMode.AUTO]

    @property
    def supported_features(self):
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        if self.values.get("season") == "heating":
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def preset_modes(self):
        return ["schedule", "away", "frost_guard"] if self.values.get("season") == "heating" else []

    @property
    def preset_mode(self):
        return {"home":"schedule", "schedule":"schedule", "away":"away", "hg":"frost_guard", "manual":"manual", "max":"boost", "off":"off"}.get(self.values.get("mode")) if self.values.get("season") == "heating" else None

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
        value = {"schedule":"schedule", "away":"away", "frost_guard":"hg"}.get(preset_mode)
        if value is None:
            raise HomeAssistantError("Modalità non supportata in questa versione")
        await self.coordinator.async_command(self.mid, "preset", value)

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode == HVACMode.AUTO and self.values.get("season") == "heating":
            await self.async_set_preset_mode("schedule")
        elif hvac_mode == self.hvac_mode:
            return
        else:
            raise HomeAssistantError("Cambio stagione e spegnimento non ancora abilitati")
