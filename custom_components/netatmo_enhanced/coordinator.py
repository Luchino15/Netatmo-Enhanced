"""Independent API client with serialized, verified commands."""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
import asyncio
import logging
from aiohttp import ClientError, ClientTimeout
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import BASE_URL, DOMAIN, SCOPES, token_scopes
from .commands import (
    BOOST_DURATIONS, DEADLINE_TOLERANCE_SECONDS, SEASON_PREFIX, command, confirmed,
    requested_duration,
)
from .cloud_push import PushState
from .model import discover, project, snapshot
from .telemetry import ApiMetrics, ControlHistory, elapsed_ms, utc_now
from .webhook_core import API_URL, MIN_REFRESH_SECONDS

_LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 600
PUSH_API_GRACE_SECONDS = 30
API_FAILURE_GRACE_SECONDS = 2 * POLL_SECONDS
COMMAND_PUSH_WAIT_SECONDS = 3
NEW_COMMANDS = {"boost", "stop_boost", "cycle_boost", "power_off", "temperature_duration"}
DEADLINE_PARTNERS = {
    f"{prefix}_{first}": f"{prefix}_{second}"
    for prefix in SEASON_PREFIX.values()
    for first, second in (("setpoint_end_time", "end_time_is_boost"), ("end_time_is_boost", "setpoint_end_time"))
}
_UNVERIFIED = object()


class TemporaryApiError(UpdateFailed):
    """A failed request that can be retried without reauthenticating."""


@dataclass
class PushObservation:
    revision: int
    changed_revision: int
    changed_at: float
    conflicting_api_value: object = _UNVERIFIED


@dataclass
class CommandObservation:
    mid: str
    before: dict
    kind: str
    value: object
    request: tuple
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    observed: set = field(default_factory=set)
    required: set = field(default_factory=set)
    armed: bool = False
    preserve_manual_override: bool = False
    minutes: int | None = None
    deadline: int | None = None
    label: str | None = None

    def matches(self, state):
        return (_same_device(self.before, state) and state.get("reachable") is not False
                and self.required <= self.observed
                and confirmed(self.before, state, self.kind, self.value, deadline=self.deadline))


def _same_device(first, second):
    return all(first.get(key) == second.get(key) for key in ("home_id", "room_id"))

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
                         update_interval=timedelta(seconds=POLL_SECONDS))
        self.oauth = oauth
        self.http = async_get_clientsession(hass)
        self.guard = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
        self.lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.metrics = ApiMetrics()
        self._refresh_requests = {}
        self._active_refreshes = {}
        self.cloud_push = False
        self._push = PushState()
        self._push_revision = 0
        self._push_fields = {}
        self._push_counts = Counter()
        self._last_push = None
        self._last_api = None
        self._last_raw_api = {}
        self._last_api_clock = None
        self._last_api_outcome = None
        self._last_api_failure = None
        self._poll_handle = None
        self._poll_task = None
        self._command_observation = None
        self._controls = ControlHistory()
        self._known_boosts = {}

    @callback
    def async_set_cloud_push(self, enabled):
        if self.cloud_push == enabled:
            return
        self.cloud_push = enabled
        if enabled:
            self._async_unsub_refresh()
            self._push = PushState()
        else:
            self._cancel_cloud_poll()
        if self._listeners:
            self._schedule_refresh()

    @callback
    def _schedule_refresh(self):
        if not self.cloud_push:
            super()._schedule_refresh()
            return
        if self.config_entry.pref_disable_polling or self._poll_handle is not None:
            return
        self._poll_handle = self.hass.loop.call_later(POLL_SECONDS, self._cloud_poll_tick)

    @callback
    def _cloud_poll_tick(self):
        self._poll_handle = None
        if (not self.cloud_push or not self._listeners or self.hass.is_stopping
                or self.config_entry.pref_disable_polling):
            return
        self._schedule_refresh()
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = self.config_entry.async_create_background_task(
                self.hass, self.async_refresh_for("periodic"), "Netatmo Enhanced safety poll"
            )

    @callback
    def _cancel_cloud_poll(self):
        if self._poll_handle is not None:
            self._poll_handle.cancel()
            self._poll_handle = None

    @callback
    def _unschedule_refresh(self):
        self._cancel_cloud_poll()
        super()._unschedule_refresh()

    async def async_shutdown(self):
        self._cancel_cloud_poll()
        await super().async_shutdown()

    @callback
    def async_apply_push(self, payload):
        """Commit the accepted fields and notify HA in the same event-loop turn."""
        previous = self.data or {}
        result = self._push.apply(previous, payload)
        now = monotonic()
        if result.accepted:
            self._push_revision += 1
            for mid, fields in result.accepted.items():
                values = result.data[mid]
                for key in fields:
                    identity = (values["home_id"], values["room_id"], mid, key)
                    observation = self._push_fields.get(identity)
                    if observation is None or values.get(key) != previous[mid].get(key):
                        self._push_fields[identity] = PushObservation(
                            self._push_revision, self._push_revision, now
                        )
                    else:
                        observation.revision = self._push_revision
            self._observe_command(result)
        if result.changed:
            stamp = utc_now()
            self._last_push = stamp.isoformat()
            self._controls.observe(previous, result.data, "push", stamp)
            self._learn_boosts(previous, result.data, result.accepted, stamp.timestamp())
            self.data = result.data
            self.async_update_listeners()
        return result

    def _observe_command(self, result):
        pending = self._command_observation
        if pending is None or not pending.armed:
            return
        fields = result.accepted.get(pending.mid, set())
        pending.observed.update(fields)
        state = result.data.get(pending.mid, {})
        if not pending.matches(state):
            pending.ready.clear()
        elif pending.required & fields:
            pending.ready.set()

    @callback
    def cloud_diagnostics(self):
        return {
            **self._push_counts,
            "last_push_at_utc": self._last_push,
            "last_api_at_utc": self._last_api,
            "last_api_outcome": self._last_api_outcome,
            "last_api_failure_reason": self._last_api_failure,
            "poll_interval_seconds": POLL_SECONDS,
            "poll_scheduled": self._poll_handle is not None if self.cloud_push else bool(self._unsub_refresh),
            "poll_disabled_in_home_assistant": self.config_entry.pref_disable_polling,
            "api_push_grace_seconds": PUSH_API_GRACE_SECONDS,
            "controls": self._controls.snapshot(),
        }

    async def async_refresh_for(self, source, timing=None):
        """Attribute a refresh to the calling task, including after lock waits."""
        task = asyncio.current_task()
        self._refresh_requests[task] = (source, timing if timing is not None else {})
        try:
            await self.async_refresh()
        finally:
            del self._refresh_requests[task]

    async def _async_refresh(self, log_failures=True, raise_on_auth_failed=False,
                             scheduled=False, raise_on_entry_error=False):
        """Measure an actual update through the coordinator's listener publication."""
        task = asyncio.current_task()
        default = "periodic" if scheduled else "startup" if raise_on_entry_error else "manual"
        source, timing = self._refresh_requests.get(task, (default, {}))
        if source == "webhook":
            ready_at = max(self.guard.get("until", 0),
                           self.guard.get("refresh_started", -float("inf")) + MIN_REFRESH_SECONDS)
            remaining = ready_at - monotonic()
            if remaining > 0:
                timing.update(outcome="deferred", retry_after_seconds=remaining,
                              retry_at_monotonic_s=ready_at)
                return
        self._active_refreshes[task] = (source, timing)
        outcome = "failed"
        try:
            await super()._async_refresh(
                log_failures=log_failures, raise_on_auth_failed=raise_on_auth_failed,
                scheduled=scheduled, raise_on_entry_error=raise_on_entry_error,
            )
            if self.last_update_success and not timing.get("retained_push_data"):
                outcome = "success"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            del self._active_refreshes[task]
            if "started_monotonic_s" in timing:
                completed = monotonic()
                timing.update(
                    outcome=outcome,
                    completed_at_utc=utc_now().isoformat(),
                    completed_monotonic_s=completed,
                    duration_ms=elapsed_ms(timing["started_monotonic_s"], completed),
                )
                if "first_http_started_monotonic_s" in timing:
                    timing["http_span_ms"] = elapsed_ms(
                        timing["first_http_started_monotonic_s"], timing["last_http_completed_monotonic_s"]
                    )
                if outcome != "success":
                    timing["failure_reason"] = (
                        "cancelled" if outcome == "cancelled" else
                        "api_cooldown" if self.guard.get("until", 0) > completed else
                        timing.get("last_http_failure", "coordinator_update_failed")
                    )
                self._last_api_outcome = outcome
                self._last_api_failure = timing.get("failure_reason") if outcome != "success" else None
                self.metrics.update(source, outcome)

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
            status, payload, rate_limit = await self._http_json(
                method, API_URL, "/webhooks/v1", "registration", 10, **kwargs,
            )
            if status == 429:
                self._record_rate_limit(rate_limit)
            return status, payload

    async def _http_json(self, method, url, endpoint, source, timeout, **kwargs):
        """Count calls that reach the HTTP client, even without a response."""
        headers = {"Authorization": f"Bearer {self.oauth.token['access_token']}"}
        _, timing = self._active_refreshes.get(asyncio.current_task(), (None, None))
        started = monotonic()
        if timing is not None:
            if "first_http_started_monotonic_s" not in timing:
                timing["first_http_started_monotonic_s"] = started
                timing["first_http_started_at_utc"] = utc_now().isoformat()
            timing["http_requests"] = timing.get("http_requests", 0) + 1
        status = None
        outcome = "transport_error"
        try:
            async with self.http.request(
                method, url, headers=headers, timeout=ClientTimeout(total=timeout),
                allow_redirects=False, raise_for_status=False, **kwargs,
            ) as response:
                status = response.status
                payload = None
                if 200 <= status < 300:
                    outcome = "success"
                    if status != 204:
                        try:
                            payload = await response.json()
                        except TimeoutError:
                            raise
                        except (ClientError, ValueError):
                            outcome = "invalid_response"
                else:
                    outcome = "http_error"
                return status, payload, response.headers.get("Retry-After")
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TimeoutError:
            outcome = "timeout"
            raise
        finally:
            completed = monotonic()
            duration = elapsed_ms(started, completed)
            self.metrics.request(source, method, endpoint, duration, status, outcome)
            if timing is not None:
                timing["last_http_completed_monotonic_s"] = completed
                if outcome != "success":
                    timing["last_http_failure"] = f"http_{status}" if outcome == "http_error" else outcome
                timing["http_duration_ms_total"] = round(
                    timing.get("http_duration_ms_total", 0.0) + duration, 3
                )

    async def _request(self, endpoint, params=None, form=None, json_body=None):
        if endpoint not in {"homesdata", "homestatus", "setstate", "setthermmode", "switchhomeschedule"}:
            raise ValueError("Unsupported endpoint")
        async with self.lock:
            remaining = self.guard.get("until", 0)-monotonic()
            if remaining > 0:
                raise TemporaryApiError(f"Netatmo API cooldown: {int(remaining)+1} seconds")
            await self.oauth.async_ensure_token_valid()
            source, _ = self._active_refreshes.get(asyncio.current_task(), ("command", None))
            # A notification received while waiting for the lock or OAuth is
            # unrelated to this write. Arm only at the HTTP send boundary.
            pending = self._command_observation
            if pending is not None and not pending.armed and endpoint not in {"homesdata", "homestatus"}:
                endpoint, form, json_body = self._arm_command(pending)
            try:
                path = f"/api/{endpoint}"
                status, payload, rate_limit = await self._http_json(
                    "POST", f"{BASE_URL}{path}", path, source, 20,
                    params=params, data=form, json=json_body,
                )
            except (ClientError, TimeoutError, ValueError):
                raise TemporaryApiError(f"Netatmo {endpoint}: network or invalid JSON") from None
            if status == 429:
                delay = self._record_rate_limit(rate_limit)
                raise TemporaryApiError(f"Netatmo API rate limit: cooldown {int(delay)} seconds")
            if status == 401:
                raise ConfigEntryAuthFailed("Netatmo authorization expired")
            if status >= 500:
                raise TemporaryApiError(f"Netatmo {endpoint}: HTTP {status}")
            if status != 200:
                raise UpdateFailed(f"Netatmo {endpoint}: HTTP {status}")
            if not isinstance(payload, dict) or payload.get("error") or payload.get("status") not in (None, "ok"):
                raise UpdateFailed(f"Netatmo {endpoint}: unexpected response")
            return payload

    async def _async_update_data(self):
        start = monotonic()
        self.guard["refresh_started"] = start
        source, timing = self._active_refreshes[asyncio.current_task()]
        timing.update(started_monotonic_s=start, started_at_utc=utc_now().isoformat())
        revision = self._push_revision
        try:
            fresh = await self._read_snapshots()
        except TemporaryApiError:
            if (self.cloud_push and self.last_update_success and self.data and self._last_api_clock is not None
                    and start - self._last_api_clock < API_FAILURE_GRACE_SECONDS):
                timing["retained_push_data"] = True
                self._push_counts["api_failures_retaining_data"] += 1
                return self.data
            raise
        self._last_api_clock = monotonic()
        self._last_api = utc_now().isoformat()
        self._last_raw_api = fresh
        if self.cloud_push or self._push_revision != revision:
            fresh = self._merge_api_push(fresh, revision, start, source)
        else:
            self._push_fields.clear()
        self._controls.observe(self.data or {}, fresh, "api", utc_now())
        for mid, (context, _) in list(self._known_boosts.items()):
            state = fresh.get(mid, {})
            if state.get("room_mode") != "max" or self._boost_context(state) != context:
                del self._known_boosts[mid]
        return fresh

    def _merge_api_push(self, fresh, revision, started, source):
        """Merge before the parent assigns data, with no intervening await."""
        if self.cloud_push:
            self._push.reconciled(fresh, started=started)
        previous = self.data or {}
        fresh = fresh.copy()
        overlays = {}
        protected = set()
        for identity, observation in list(self._push_fields.items()):
            home, room, mid, key = identity
            old = previous.get(mid)
            new = fresh.get(mid)
            if old is None or (old["home_id"], old["room_id"]) != (home, room):
                del self._push_fields[identity]
                continue
            in_flight = observation.revision > revision
            if new is None or not _same_device(old, new):
                # A fresh topology can remove a room or move a device. Keep a
                # concurrently updated snapshot only until a later read.
                if observation.changed_revision > revision:
                    overlays[mid] = old.copy()
                    protected.add(identity)
                else:
                    del self._push_fields[identity]
                continue
            recent_change = (observation.changed_revision > revision
                             or started - observation.changed_at < PUSH_API_GRACE_SECONDS)
            old_value, new_value = old.get(key), new.get(key)
            if not recent_change and not in_flight:
                del self._push_fields[identity]
            elif old_value == new_value:
                observation.conflicting_api_value = _UNVERIFIED
            elif (recent_change or observation.conflicting_api_value != new_value):
                # No-op pushes protect a read in flight, but two later reads
                # agreeing with each other can correct a long-unchanged field.
                if not recent_change:
                    observation.conflicting_api_value = new_value
                overlay = overlays.setdefault(mid, new.copy())
                overlay[key] = old_value
                if key in {"boiler_status", "cooler_status"}:
                    overlay.update(boiler_status=old.get("boiler_status"),
                                   cooler_status=old.get("cooler_status"))
                if key in DEADLINE_PARTNERS:
                    partner = DEADLINE_PARTNERS[key]
                    overlay[partner] = old.get(partner)
                protected.add(identity)
            else:
                del self._push_fields[identity]
        if protected:
            self._push_counts["api_responses_with_protected_fields"] += 1
            for mid, values in overlays.items():
                old = previous[mid]
                for season in ("heating", "cooling"):
                    sid = values.get(f"{season}_schedule_id")
                    names = f"{season}_schedule_names"
                    options = f"{season}_schedule_options"
                    if sid in old.get(names, {}) and sid not in values.get(names, {}):
                        values[names] = old[names]
                        values[options] = old[options]
                fresh[mid] = project(values)
            if (self.cloud_push and source != "webhook"
                    and (adapter := getattr(self, "webhook_v1", None)) is not None):
                adapter.async_queue_reconciliation("api_push_divergence")
        return fresh

    async def _read_snapshots(self):
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
        old = (self.data or {}).get(mid, {})
        is_new_auto = (kind == "preset" and value == "schedule"
                       and (old.get("season") == "cooling" or old.get("room_mode") == "max"))
        if getattr(self, "cloud_push", False) or kind in NEW_COMMANDS or is_new_auto:
            await self._async_push_command(mid, kind, value)
            return
        async with self.command_lock:
            old = (self.data or {}).get(mid, {})
            if not self.last_update_success or not old:
                raise HomeAssistantError("Dati non disponibili: aggiorna prima il termostato")
            expected_season = old.get("season")
            await self.async_refresh_for("command")
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
            await self.async_refresh_for("command")
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
                await self.async_refresh_for("command")
            if not self.last_update_success or not confirmed(fresh, (self.data or {}).get(mid, {}), kind, value):
                raise HomeAssistantError("Comando accettato dalle API, ma stato non ancora confermato. Controlla l'app e aggiorna i dati; non ripetere subito il comando.")

    async def _async_push_command(self, mid, kind, value):
        async with self.command_lock:
            old = (self.data or {}).get(mid, {})
            if not self.last_update_success or not old:
                raise HomeAssistantError("Dati non disponibili: aggiorna prima il termostato")
            timing = {}
            await self.async_refresh_for("command", timing)
            before = (self.data or {}).get(mid, {})
            if (timing.get("outcome") != "success" or not _same_device(old, before)
                    or before.get("season") != old.get("season")):
                raise HomeAssistantError("Stato o stagione cambiati: controlla i valori e riprova")

            label = kind if kind in NEW_COMMANDS else None
            if kind == "preset" and value == "schedule" and before.get("season") == "cooling":
                label = "cooling_auto"
            if kind == "cycle_boost":
                kind, value = self._next_boost(mid, before)
            after, raw, endpoint = await self._write_and_verify(
                mid, before, kind, value, label=label,
                preserve_manual_override=label in {"cycle_boost", "stop_boost"},
            )
            if (kind == "preset" and value == "schedule" and endpoint == "setthermmode"
                    and self._needs_room_return(before, after, raw)):
                after, _, _ = await self._write_and_verify(mid, after, kind, value, preserve_manual_override=True)
            if not after or (kind == "preset" and not confirmed(before, after, kind, value)):
                self._push_counts["commands_unconfirmed"] += 1
                raise HomeAssistantError("Comando accettato dalle API, ma stato non ancora confermato. Controlla l'app e aggiorna i dati; non ripetere subito il comando.")

    @staticmethod
    def _boost_context(state):
        return tuple(state.get(key) for key in ("home_id", "room_id", "season", "setpoint_end_time"))

    def _learn_boosts(self, previous, current, accepted, received):
        for mid, state in current.items():
            old = previous.get(mid, {})
            expiry = state.get("setpoint_end_time")
            if state.get("room_mode") != "max" or type(expiry) not in (int, float):
                self._known_boosts.pop(mid, None)
            elif old.get("room_mode") != "max" or self._boost_context(old) != self._boost_context(state):
                self._known_boosts.pop(mid, None)
                prefix = SEASON_PREFIX[state["season"]]
                if f"{prefix}_setpoint_end_time" not in accepted.get(mid, ()):
                    continue
                for minutes in BOOST_DURATIONS:
                    if abs(expiry - received - minutes * 60) <= DEADLINE_TOLERANCE_SECONDS:
                        self._known_boosts[mid] = (self._boost_context(state), minutes)
                        break

    def _next_boost(self, mid, current):
        if current.get("room_mode") != "max":
            return "boost", BOOST_DURATIONS[0]
        known = self._known_boosts.get(mid)
        if (known is None or known[0] != self._boost_context(current)
                or current["setpoint_end_time"] <= utc_now().timestamp()):
            raise HomeAssistantError("Durata Boost sconosciuta: scegli 30, 60 o 90 minuti oppure interrompilo")
        step = BOOST_DURATIONS.index(known[1]) + 1
        return ("boost", BOOST_DURATIONS[step]) if step < len(BOOST_DURATIONS) else ("stop_boost", None)

    def _arm_command(self, pending):
        current = (self.data or {}).get(pending.mid, {})
        if not SCOPES.issubset(token_scopes(self.oauth.token)):
            raise HomeAssistantError("Permessi di scrittura mancanti: riautenticare l'integrazione")
        if (not _same_device(pending.before, current)
                or current.get("season") != pending.before.get("season")
                or (pending.preserve_manual_override
                    and not self._same_override(pending.before, current))
                or command(current, pending.kind, pending.value, deadline=pending.deadline) != pending.request):
            raise HomeAssistantError("Stato o stagione cambiati: controlla i valori e riprova")
        if pending.minutes is not None:
            pending.deadline = int(utc_now().timestamp()) + pending.minutes * 60
            pending.request = command(current, pending.kind, pending.value, deadline=pending.deadline)
        pending.armed = True
        return pending.request

    async def _write_and_verify(self, mid, before, kind, value, *, preserve_manual_override=False, label=None):
        try:
            minutes = requested_duration(kind, value)
            deadline = int(utc_now().timestamp()) + minutes * 60 if minutes is not None else None
            request = command(before, kind, value, deadline=deadline)
        except ValueError as exc:
            raise HomeAssistantError(str(exc)) from exc
        endpoint, form, body = request
        pending = CommandObservation(mid, before, kind, value, request,
                                     preserve_manual_override=preserve_manual_override,
                                     minutes=minutes, deadline=deadline, label=label)
        prefix = SEASON_PREFIX[before["season"]]
        if kind in {"temperature", "temperature_duration"}:
            pending.required = {f"{prefix}_setpoint_mode", f"{prefix}_setpoint_temperature"}
            if kind == "temperature_duration":
                pending.required.add(f"{prefix}_setpoint_end_time")
        elif kind in {"boost", "stop_boost"}:
            pending.required = {f"{prefix}_setpoint_mode", f"{prefix}_setpoint_end_time"}
        elif kind == "schedule":
            pending.required = {"heating_schedule_id" if prefix == "therm" else "cooling_schedule_id"}
        else:
            pending.required = {f"{prefix}_setpoint_mode" if endpoint == "setstate" else "therm_mode"}
        self._command_observation = pending
        try:
            try:
                result = await self._request(endpoint, form=form, json_body=body)
            except (UpdateFailed, ValueError, ConfigEntryAuthFailed) as exc:
                raise HomeAssistantError(str(exc)) from exc
            response_body = result.get("body")
            if result.get("status") != "ok" or (isinstance(response_body, dict) and response_body.get("errors")):
                raise HomeAssistantError("Netatmo non ha confermato il comando")
            if self.cloud_push:
                try:
                    async with asyncio.timeout(COMMAND_PUSH_WAIT_SECONDS):
                        await pending.ready.wait()
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(COMMAND_PUSH_WAIT_SECONDS)
            after = (self.data or {}).get(mid, {})
            if pending.ready.is_set() and pending.matches(after):
                self._verified_command(pending, after, "push")
                return after, {}, endpoint

            self._push_counts["command_api_verifications"] += 1
            timing = {}
            await self.async_refresh_for("command", timing)
            after = (self.data or {}).get(mid, {})
            raw = self._last_raw_api.get(mid, {})
            if pending.ready.is_set() and pending.matches(after):
                self._verified_command(pending, after, "push")
                return after, {}, endpoint
            if (timing.get("outcome") == "success" and _same_device(before, after)
                    and _same_device(before, raw) and after.get("reachable") is not False
                    and raw.get("reachable") is not False):
                if (confirmed(before, raw, kind, value, deadline=pending.deadline)
                        and confirmed(before, after, kind, value, deadline=pending.deadline)):
                    self._verified_command(pending, after, "api")
                    return after, raw, endpoint
                if kind == "preset" and value == "schedule" and endpoint == "setthermmode":
                    return after, raw, endpoint
            self._controls.command(pending.label, "unconfirmed", minutes=pending.minutes)
            return {}, {}, endpoint
        except HomeAssistantError:
            self._controls.command(pending.label, "failed", minutes=pending.minutes)
            raise
        finally:
            self._command_observation = None

    def _verified_command(self, pending, after, source):
        self._push_counts[f"commands_confirmed_by_{source}"] += 1
        self._controls.command(pending.label, source, minutes=pending.minutes)
        if pending.kind == "boost":
            self._known_boosts[pending.mid] = (self._boost_context(after), pending.minutes)

    def _needs_room_return(self, before, after, raw):
        """Only an API-confirmed intermediate state can lead to a second write."""
        return (
            bool(raw) and before.get("room_mode") in {"manual", "max"}
            and all(state.get("season") == before["season"]
                    and state.get("home_mode") in {"home", "schedule"}
                    and self._same_override(before, state)
                    for state in (after, raw))
        )

    @staticmethod
    def _same_override(before, after):
        return all(before.get(key) == after.get(key)
                   for key in ("room_mode", "target_temperature", "setpoint_end_time"))
