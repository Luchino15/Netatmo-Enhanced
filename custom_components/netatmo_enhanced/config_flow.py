"""OAuth configuration with explicit write permissions."""
import logging
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from .const import DOMAIN, SCOPES
from .webhook_core import MODE_KEY, MODE_OFF, MODE_RECEIVE, MODE_REFRESH

class NetatmoEnhancedFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    DOMAIN = DOMAIN

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WebhookOptionsFlow()

    @property
    def logger(self):
        return logging.getLogger(__name__)

    @property
    def extra_authorize_data(self):
        return {"scope": " ".join(sorted(SCOPES))}

    async def async_step_user(self, user_input=None):
        if self.source != SOURCE_REAUTH and self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return await super().async_step_user(user_input)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data):
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Netatmo Enhanced", data=data)


class WebhookOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    MODE_KEY, default=self.config_entry.options.get(MODE_KEY, MODE_OFF)
                ): vol.In({
                    MODE_OFF: "Disattivato",
                    MODE_RECEIVE: "Solo ricezione",
                    MODE_REFRESH: "Ricezione e aggiornamento API",
                }),
            }),
        )
