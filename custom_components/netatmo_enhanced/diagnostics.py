"""Anonymized diagnostics limited to webhook counters."""


async def async_get_config_entry_diagnostics(hass, entry):
    return {"webhook_v1": entry.runtime_data.webhook_v1.diagnostics()}
