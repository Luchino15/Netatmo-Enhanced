"""Netatmo Enhanced, standalone Smarther integration."""
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session, async_get_config_entry_implementation,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from .const import SCOPES, token_scopes
from .coordinator import EnhancedCoordinator
from .webhook_core import MODE_KEY, MODE_OFF
from .webhook_v1 import WebhookV1

PLATFORMS = [Platform.SENSOR, Platform.CLIMATE, Platform.SELECT]

async def async_setup_entry(hass, entry):
    if not SCOPES.issubset(token_scopes(entry.data.get("token", {}))):
        raise ConfigEntryAuthFailed("Netatmo Enhanced requires read and write permissions")
    implementation = await async_get_config_entry_implementation(hass, entry)
    oauth = OAuth2Session(hass, entry, implementation)
    coordinator = EnhancedCoordinator(hass, entry, oauth)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.webhook_v1 = WebhookV1(hass, entry, coordinator)
    entry.async_on_unload(coordinator.webhook_v1.stop)
    entry.async_on_unload(entry.add_update_listener(_async_update_webhook_mode))
    coordinator.webhook_v1.start()
    return True


async def _async_update_webhook_mode(_hass, entry):
    if entry.state is ConfigEntryState.LOADED:
        await entry.runtime_data.webhook_v1.async_set_mode(entry.options.get(MODE_KEY, MODE_OFF))


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
