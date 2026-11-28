"""Home Assistant actions targeting Netatmo Enhanced climate entities."""

import probatio

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import callback
from homeassistant.helpers.service import async_register_platform_entity_service

from .commands import BOOST_DURATIONS, MANUAL_MAX_MINUTES
from .const import DOMAIN


def _boost_duration(value):
    if type(value) not in (int, float) or value not in BOOST_DURATIONS:
        raise probatio.Invalid("Boost duration must be 30, 60, or 90 minutes")
    return int(value)


def _manual_duration(value):
    if type(value) not in (int, float) or not 1 <= value <= MANUAL_MAX_MINUTES or value % 1 != 0:
        raise probatio.Invalid(
            f"Manual duration must be a whole number of minutes from 1 to {MANUAL_MAX_MINUTES}"
        )
    return int(value)


def _temperature(value):
    if type(value) not in (int, float) or not 7 <= value <= 30 or value * 2 % 1 != 0:
        raise probatio.Invalid("Temperature must be 7–30 °C in half-degree increments")
    return value


@callback
def async_setup_services(hass):
    actions = {
        "set_boost": {probatio.Required("duration"): _boost_duration},
        "stop_boost": None,
        "cycle_boost": None,
        "set_manual_temperature": {
            probatio.Required(ATTR_TEMPERATURE): _temperature,
            probatio.Required("duration"): _manual_duration,
        },
    }
    for action, schema in actions.items():
        async_register_platform_entity_service(
            hass, DOMAIN, action, entity_domain=CLIMATE_DOMAIN,
            func=f"async_{action}", schema=schema,
        )
