"""Personal Netatmo application credentials."""
from homeassistant.components.application_credentials import AuthorizationServer
from .const import BASE_URL

async def async_get_authorization_server(hass):
    return AuthorizationServer(
        authorize_url=f"{BASE_URL}/oauth2/authorize",
        token_url=f"{BASE_URL}/oauth2/token",
    )
