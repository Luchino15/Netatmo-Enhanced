DOMAIN = "netatmo_enhanced"
BASE_URL = "https://api.netatmo.com"

SCOPES = frozenset({"read_smarther", "read_thermostat", "write_smarther", "write_thermostat"})

def token_scopes(token):
    value = token.get("scope", [])
    return set(value.split() if isinstance(value, str) else value or [])
