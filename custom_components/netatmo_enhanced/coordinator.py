"""Independent API client with serialized, verified commands."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
import asyncio
import logging
from aiohttp import ClientError, ClientTimeout
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import BASE_URL, DOMAIN, SCOPES, token_scopes
from .commands import command, confirmed
from .model import discover, snapshot
from .webhook_core import API_URL

_LOGGER = logging.getLogger(__name__)

def retry_delay(header):
    try:
        value = float(header)
        if value >= 0 and value < float("inf"):
            return max(900, value)
    except (ValueError, TypeError):
        pass
    try:
        value = parsedate_to_datetime(header)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(900, (value-datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError, AttributeError):
        return 900

class EnhancedCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, oauth):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=timedelta(minutes=10))
        self.oauth = oauth
        self.http = async_get_clientsession(hass)
        self.guard = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
        self.lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()

    async def _read(self, endpoint, params=None):
        payload = await self._request(endpoint, params=params)
        if not isinstance(payload.get("body"), dict):
            raise UpdateFailed(f"Netatmo {endpoint}: unexpected response")
        return payload["body"]

    def _record_rate_limit(self, header):
        delay = max(retry_delay(header), self.guard.get("delay", 450) * 2)
        self.guard.update(until=monotonic() + delay, delay=min(delay, 3600))
        return delay

    async def async_webhook_request(self, method, body=None):
        """Use the existing OAuth session and API cooldown for registration."""
        if method not in {"GET", "POST"}:
            raise ValueError("Unsupported webhook method")
        async with self.lock:
            if self.guard.get("until", 0) > monotonic():
                return 429, None
            await self.oauth.async_ensure_token_valid()
            kwargs = {"json": body} if method == "POST" else {}
            async with self.http.request(
                method, API_URL, **kwargs,
                headers={"Authorization": f"Bearer {self.oauth.token['access_token']}"},
                timeout=ClientTimeout(total=10), allow_redirects=False,
                raise_for_status=False,
            ) as response:
                if response.status == 429:
                    self._record_rate_limit(response.headers.get("Retry-After"))
                return response.status, await response.json() if response.status == 200 else None

    async def _request(self, endpoint, params=None, form=None, json_body=None):
        if endpoint not in {"homesdata", "homestatus", "setstate", "setthermmode", "switchhomeschedule"}:
            raise ValueError("Unsupported endpoint")
        async with self.lock:
            remaining = self.guard.get("until", 0)-monotonic()
            if remaining > 0:
                raise UpdateFailed(f"Netatmo API cooldown: {int(remaining)+1} seconds")
            await self.oauth.async_ensure_token_valid()
            try:
                async with self.http.post(
                    f"{BASE_URL}/api/{endpoint}",
                    params=params,
                    data=form,
                    json=json_body,
                    headers={"Authorization": f"Bearer {self.oauth.token['access_token']}"},
                    timeout=ClientTimeout(total=20),
                    allow_redirects=False,
                ) as response:
                    if response.status == 429:
                        delay = self._record_rate_limit(response.headers.get("Retry-After"))
                        raise UpdateFailed(f"Netatmo API rate limit: cooldown {int(delay)} seconds")
                    if response.status == 401:
                        raise ConfigEntryAuthFailed("Netatmo authorization expired")
                    if response.status != 200:
                        raise UpdateFailed(f"Netatmo {endpoint}: HTTP {response.status}")
                    payload = await response.json()
            except (ClientError, TimeoutError, ValueError) as exc:
                raise UpdateFailed(f"Netatmo {endpoint}: network or invalid JSON") from exc
            if not isinstance(payload, dict) or payload.get("error") or payload.get("status") not in (None, "ok"):
                raise UpdateFailed(f"Netatmo {endpoint}: unexpected response")
            return payload

    async def _async_update_data(self):
        self.guard["refresh_started"] = monotonic()
        # Home mode, season and selected schedules are mutable.
        # Never reuse an hour-old topology for a manual or periodic refresh.
        body = await self._read("homesdata")
        if not isinstance(body.get("homes"), list):
            raise UpdateFailed("Netatmo: missing home inventory")
        devices = discover(body["homes"])
        if not devices:
            raise UpdateFailed("No BTicino Smarther BNS found with granted read scopes")
        statuses = {}
        result = {}
        for (hid, mid), (home, room, module) in devices.items():
            if hid not in statuses:
                body = await self._read("homestatus", {"home_id": hid})
                status = body.get("home")
                if not isinstance(status, dict) or status.get("id") not in (None, hid):
                    raise UpdateFailed("Netatmo: invalid home status")
                statuses[hid] = status
            result[mid] = snapshot(home, statuses[hid], room, module)
        self.guard.pop("delay", None)
        _LOGGER.debug("Update (homesdata + homestatus): %s Smarther device(s)", len(result))
        return result

    async def async_command(self, mid, kind, value):
        async with self.command_lock:
            old = (self.data or {}).get(mid, {})
            if not self.last_update_success or not old:
                raise HomeAssistantError("Dati non disponibili: aggiorna prima il termostato")
            expected_season = old.get("season")
            await self.async_refresh()
            fresh = (self.data or {}).get(mid, {})
            if not self.last_update_success or not fresh or fresh.get("season") != expected_season:
                raise HomeAssistantError("Stato o stagione cambiati: controlla i valori e riprova")
            if not SCOPES.issubset(token_scopes(self.oauth.token)):
                raise HomeAssistantError("Permessi di scrittura mancanti: riautenticare l'integrazione")
            try:
                endpoint, form, body = command(fresh, kind, value)
                result = await self._request(endpoint, form=form, json_body=body)
            except (UpdateFailed, ValueError, ConfigEntryAuthFailed) as exc:
                raise HomeAssistantError(str(exc)) from exc
            response_body = result.get("body")
            if result.get("status") != "ok" or (isinstance(response_body, dict) and response_body.get("errors")):
                raise HomeAssistantError("Netatmo non ha confermato il comando")
            await asyncio.sleep(3)
            await self.async_refresh()
            after = (self.data or {}).get(mid, {})
            # A global preset can coexist with a room override. Only after
            # confirming the home has returned to schedule, clear that override.
            # Never repeat the same command or continue after a failed refresh.
            if (kind == "preset" and value == "schedule" and endpoint == "setthermmode"
                    and self.last_update_success and after.get("season") == expected_season
                    and after.get("home_mode") in {"home", "schedule"}
                    and after.get("room_mode") == "manual"
                    and after.get("reachable") is not False
                    and after.get("home_id") == fresh.get("home_id")
                    and after.get("room_id") == fresh.get("room_id")):
                try:
                    second_endpoint, form, body = command(after, kind, value)
                    result = await self._request(second_endpoint, form=form, json_body=body)
                except (UpdateFailed, ValueError, ConfigEntryAuthFailed) as exc:
                    raise HomeAssistantError(str(exc)) from exc
                response_body = result.get("body")
                if result.get("status") != "ok" or (isinstance(response_body, dict) and response_body.get("errors")):
                    raise HomeAssistantError("Programmazione casa ripristinata, ma annullamento manuale non confermato")
                await asyncio.sleep(3)
                await self.async_refresh()
            if not self.last_update_success or not confirmed(fresh, (self.data or {}).get(mid, {}), kind, value):
                raise HomeAssistantError("Comando accettato dalle API, ma stato non ancora confermato. Controlla l'app e aggiorna i dati; non ripetere subito il comando.")
