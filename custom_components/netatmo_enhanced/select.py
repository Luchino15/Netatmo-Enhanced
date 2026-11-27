"""Select schedules within the confirmed current season."""
from homeassistant.components.select import SelectEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data
    # One selector per home, using its first known BNS as the device anchor.
    homes = {}
    for mid, state in c.data.items():
        homes.setdefault(state["home_id"], mid)
    async_add_entities(EnhancedSchedule(c, mid, hid) for hid, mid in homes.items())

class EnhancedSchedule(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Selezione programmazione"

    def __init__(self, coordinator, mid, hid):
        super().__init__(coordinator)
        self.mid = mid
        self._attr_unique_id = f"{hid}_schedule_select"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, mid)})

    @property
    def values(self):
        return (self.coordinator.data or {}).get(self.mid, {})

    @property
    def available(self):
        return super().available and bool(self.options) and self.values.get("reachable") is not False

    @property
    def options(self):
        return list(self.values.get("schedule_options", {}))

    @property
    def current_option(self):
        sid = self.values.get("active_schedule_id")
        return next((name for name, value in self.values.get("schedule_options", {}).items() if value == sid), None)

    async def async_select_option(self, option):
        await self.coordinator.async_command(self.mid, "schedule", option)
