"""Export sanitized webhook history and API counters."""


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    return {"webhook_v1": coordinator.webhook_v1.diagnostics(),
            "netatmo_api": coordinator.metrics.snapshot(),
            "cloud_push": coordinator.cloud_diagnostics()}
