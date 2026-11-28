"""Exercise timed and temporary Smarther commands with signed synthetic events."""

import asyncio
from datetime import UTC, datetime, timedelta
from time import time
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from push_harness import PushHarness
from push_payloads import HOME, MODULE, ROOM, room_event, season_event


READ = ["homesdata", "homestatus"]
WRITE = "setstate"


class BoostRuntimeTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.harness = await PushHarness.create()
        self.addAsyncCleanup(self.harness.stop)
        self.harness.api_calls.clear()
        self.harness.api_payloads.clear()
        self.coordinator = self.harness.coordinator
        timeout = patch.object(self.harness.coordinator_module, "COMMAND_PUSH_WAIT_SECONDS", 0.05)
        timeout.start()
        self.addCleanup(timeout.stop)

    async def run_command(self, kind, value):
        await asyncio.wait_for(self.coordinator.async_command(MODULE, kind, value), 1)

    async def event(self, sequence, **fields):
        self.assertEqual(await self.harness.push(room_event(sequence, **fields)), 200)

    async def read_current(self, *, season=None, home_mode=None, **fields):
        if season is not None:
            self.harness.home["temperature_control_mode"] = season
        if home_mode is not None:
            self.harness.home["therm_mode"] = home_mode
        self.harness.status["rooms"][0].update(fields)
        await self.coordinator.async_refresh_for("periodic")
        self.harness.api_calls.clear()
        self.harness.api_payloads.clear()

    @property
    def state(self):
        return self.harness.state[MODULE]

    def written_room(self, kwargs):
        body = kwargs["json"]
        rooms = body["home"]["rooms"]
        self.assertEqual(body, {"home": {"id": HOME, "rooms": rooms}})
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["id"], ROOM)
        return {key: value for key, value in rooms[0].items() if key != "id"}

    def assert_deadline(self, deadline, started_at, minutes):
        self.assertIs(type(deadline), int)
        self.assertGreaterEqual(deadline, started_at + minutes * 60)
        self.assertLessEqual(deadline, int(time()) + minutes * 60)

    def observed_boost(self, fields):
        prefix = "therm" if "therm_setpoint_mode" in fields else "cooling"
        return {**fields, f"{prefix}_setpoint_temperature": None}

    async def test_early_boost_webhook_publishes_before_http_and_confirms_after_http(self):
        write_started = asyncio.Event()
        allow_response = asyncio.Event()
        started_at = int(time())

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                deadline = fields["therm_setpoint_end_time"]
                self.assertEqual(fields, {
                    "therm_setpoint_mode": "max", "therm_setpoint_end_time": deadline,
                })
                self.assert_deadline(deadline, started_at, 30)
                await self.event(100, **self.observed_boost(fields))
                write_started.set()
                await allow_response.wait()

        self.harness.http_hook = hook
        command = asyncio.create_task(self.run_command("boost", 30))
        try:
            await asyncio.wait_for(write_started.wait(), 1)
            self.assertEqual(self.state["room_mode"], "max")
            self.assertIsNone(self.state["target_temperature"])
            self.assertEqual(self.harness.api_calls, READ + [WRITE])
            self.assertFalse(command.done())
        finally:
            allow_response.set()
            await command
        self.assertEqual(self.harness.api_calls, READ + [WRITE])
        self.assertEqual(self.coordinator.cloud_diagnostics()["commands_confirmed_by_push"], 1)

    async def test_boost_webhook_after_response_avoids_post_write_reads(self):
        write_finished = asyncio.Event()
        fields = {}

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields.update(self.written_room(kwargs))
                asyncio.get_running_loop().call_soon(write_finished.set)

        self.harness.http_hook = hook
        command = asyncio.create_task(self.run_command("boost", 60))
        try:
            await asyncio.wait_for(write_finished.wait(), 1)
            self.assertEqual(self.state["room_mode"], "home")
            self.assertEqual(self.harness.api_calls, READ + [WRITE])
            await self.event(100, **self.observed_boost(fields))
            await command
        finally:
            command.cancel()
            await asyncio.gather(command, return_exceptions=True)
        self.assertEqual(self.state["room_mode"], "max")
        self.assertEqual(self.state["setpoint_end_time"], fields["therm_setpoint_end_time"])
        self.assertEqual(self.harness.api_calls, READ + [WRITE])

    async def test_consecutive_boost_30_60_90_each_require_current_deadline(self):
        deadlines = []

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                deadlines.append(fields["therm_setpoint_end_time"])
                observed = self.observed_boost(fields)
                self.harness.status["rooms"][0].update(observed)
                await self.event(100 + len(deadlines), **observed)

        self.harness.http_hook = hook
        for minutes in (30, 60, 90):
            with self.subTest(minutes=minutes):
                started_at = int(time())
                await self.run_command("boost", minutes)
                self.assertEqual(self.state["setpoint_end_time"], deadlines[-1])
                self.assert_deadline(deadlines[-1], started_at, minutes)
        self.assertEqual(self.harness.api_calls, (READ + [WRITE]) * 3)
        self.assertEqual(self.coordinator.cloud_diagnostics()["commands_confirmed_by_push"], 3)

    async def test_boost_cycle_advances_then_stops_and_restarts(self):
        sequence = 100
        duration = None

        async def hook(endpoint, kwargs):
            if endpoint != WRITE:
                return None
            fields = self.written_room(kwargs)
            if duration is None:
                self.assertEqual(fields, {"therm_setpoint_mode": "home"})
                fields["therm_setpoint_end_time"] = None
                fields["therm_setpoint_temperature"] = 20
            else:
                self.assertEqual(fields["therm_setpoint_mode"], "max")
                self.assert_deadline(fields["therm_setpoint_end_time"], started_at, duration)
                fields = self.observed_boost(fields)
            self.harness.status["rooms"][0].update(fields)
            await self.event(sequence, **fields)

        self.harness.http_hook = hook
        for duration in (30, 60, 90, None, 30):
            with self.subTest(duration=duration):
                sequence += 1
                started_at = int(time())
                await self.run_command("cycle_boost", None)
                self.assertEqual(self.state["room_mode"], "home" if duration is None else "max")
        self.assertEqual(self.harness.api_calls, (READ + [WRITE]) * 5)

    async def test_stop_clears_expiry_and_preserves_existing_global_mode(self):
        await self.read_current(home_mode="away", therm_setpoint_mode="max",
                                therm_setpoint_end_time=int(time()) + 1800,
                                therm_setpoint_temperature=None)

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                self.assertEqual(self.written_room(kwargs), {"therm_setpoint_mode": "home"})
                await self.event(100, therm_setpoint_mode="home", therm_setpoint_end_time=None,
                                 therm_setpoint_temperature=20)

        self.harness.http_hook = hook
        await self.run_command("stop_boost", None)
        self.assertEqual(self.state["room_mode"], "home")
        self.assertEqual(self.state["home_mode"], "away")
        self.assertEqual(self.state["mode"], "away")
        self.assertIsNone(self.state["setpoint_end_time"])
        self.assertEqual(self.harness.api_calls, READ + [WRITE])

    async def test_heating_auto_returns_global_mode_then_clears_confirmed_boost(self):
        await self.read_current(
            home_mode="away", therm_setpoint_mode="max", therm_setpoint_temperature=None,
            therm_setpoint_end_time=int(time()) + 1800,
        )

        async def hook(endpoint, kwargs):
            if endpoint == "setthermmode":
                self.assertEqual(kwargs["data"], {"home_id": HOME, "mode": "schedule"})
                self.harness.home["therm_mode"] = "schedule"
            elif endpoint == WRITE:
                self.assertEqual(self.written_room(kwargs), {"therm_setpoint_mode": "home"})
                await self.event(100, therm_setpoint_mode="home", therm_setpoint_temperature=19,
                                 therm_setpoint_end_time=None)

        self.harness.http_hook = hook
        await self.run_command("preset", "schedule")
        self.assertEqual(self.harness.api_calls, READ + ["setthermmode"] + READ + [WRITE])
        self.assertEqual((self.state["home_mode"], self.state["room_mode"]), ("schedule", "home"))
        self.assertIsNone(self.state["setpoint_end_time"])

    async def test_boost_with_unknown_original_duration_does_not_cycle(self):
        await self.read_current(therm_setpoint_mode="max", therm_setpoint_temperature=None,
                                therm_setpoint_end_time=int(time()) + 1800)
        with self.assertRaises(self.harness.coordinator_module.HomeAssistantError):
            await self.run_command("cycle_boost", None)
        self.assertEqual(self.state["room_mode"], "max")
        self.assertNotIn(WRITE, self.harness.api_calls)

    async def test_stop_webhook_without_expiry_clear_requires_api_confirmation(self):
        await self.read_current(therm_setpoint_mode="max", therm_setpoint_temperature=None,
                                therm_setpoint_end_time=int(time()) + 1800)

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                self.harness.status["rooms"][0].update(
                    **fields, therm_setpoint_temperature=20, therm_setpoint_end_time=None,
                )
                await self.event(100, **fields, therm_setpoint_temperature=20)

        self.harness.http_hook = hook
        await self.run_command("stop_boost", None)
        self.assertEqual(self.state["room_mode"], "home")
        self.assertIsNone(self.state["setpoint_end_time"])
        self.assertEqual(self.harness.api_calls, READ + [WRITE] + READ)
        self.assertEqual(self.coordinator.cloud_diagnostics()["commands_confirmed_by_api"], 1)

    async def test_no_boost_webhook_confirms_through_one_api_fallback(self):
        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                fields["therm_setpoint_end_time"] += 30
                self.harness.status["rooms"][0].update(self.observed_boost(fields))

        self.harness.http_hook = hook
        await self.run_command("boost", 90)
        self.assertEqual(self.state["room_mode"], "max")
        self.assertEqual(self.harness.api_calls, READ + [WRITE] + READ)
        self.assertEqual(self.coordinator.cloud_diagnostics()["commands_confirmed_by_api"], 1)

    async def test_boost_in_receive_only_mode_uses_confirmed_api_state(self):
        await self.harness.adapter.async_set_mode("receive")

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.observed_boost(self.written_room(kwargs))
                self.harness.status["rooms"][0].update(fields)
                await self.event(100, **fields)
                self.assertEqual(self.state["room_mode"], "home")

        self.harness.http_hook = hook
        await self.run_command("boost", 30)
        self.assertEqual(self.state["room_mode"], "max")
        self.assertEqual(self.harness.api_calls, READ + [WRITE] + READ)
        self.assertEqual(self.coordinator.cloud_diagnostics()["commands_confirmed_by_api"], 1)

    async def test_boost_deadline_is_calculated_at_write_after_oauth_wait(self):
        now = datetime(2034, 3, 21, tzinfo=UTC)

        def clock():
            return now

        async def ensure_token():
            nonlocal now
            if self.harness.api_calls == READ:
                now += timedelta(seconds=125)

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                self.assertEqual(fields["therm_setpoint_end_time"], int(now.timestamp()) + 1800)
                await self.event(100, **self.observed_boost(fields))

        self.harness.oauth.async_ensure_token_valid.side_effect = ensure_token
        self.harness.http_hook = hook
        with patch.object(self.harness.coordinator_module, "utc_now", clock):
            await self.run_command("boost", 30)
        self.assertEqual(self.state["setpoint_end_time"], int(now.timestamp()) + 1800)
        self.assertEqual(self.harness.api_calls, READ + [WRITE])

    async def test_boost_mode_without_new_deadline_cannot_confirm_extension(self):
        old_deadline = int(time()) + 1800
        await self.read_current(therm_setpoint_mode="max", therm_setpoint_end_time=old_deadline,
                                therm_setpoint_temperature=None)

        async def hook(endpoint, _):
            if endpoint == WRITE:
                await self.event(100, therm_setpoint_mode="max")

        self.harness.http_hook = hook
        with self.assertRaises(self.harness.coordinator_module.HomeAssistantError):
            await self.run_command("boost", 60)
        self.assertEqual(self.state["setpoint_end_time"], old_deadline)
        self.assertEqual(self.harness.api_calls, READ + [WRITE] + READ)

    async def test_wrong_boost_expiry_from_signed_event_cannot_confirm_command(self):
        actual_expiry = None

        async def hook(endpoint, kwargs):
            nonlocal actual_expiry
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                actual_expiry = fields["therm_setpoint_end_time"] - 1800
                await self.event(100, therm_setpoint_mode="max", therm_setpoint_end_time=actual_expiry,
                                 therm_setpoint_temperature=None)

        self.harness.http_hook = hook
        with self.assertRaises(self.harness.coordinator_module.HomeAssistantError):
            await self.run_command("boost", 90)
        self.assertEqual(self.state["room_mode"], "max")
        self.assertEqual(self.state["setpoint_end_time"], actual_expiry)
        self.assertEqual(self.harness.api_calls, READ + [WRITE] + READ)

    async def test_rejected_boost_reports_error_even_after_matching_push(self):
        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                await self.event(100, **self.observed_boost(self.written_room(kwargs)))
                return 400, None, {}

        self.harness.http_hook = hook
        with self.assertRaises(self.harness.coordinator_module.HomeAssistantError):
            await self.run_command("boost", 60)
        self.assertEqual(self.state["room_mode"], "max")
        self.assertEqual(self.harness.api_calls, READ + [WRITE])
        self.assertNotIn("commands_confirmed_by_push", self.coordinator.cloud_diagnostics())

    async def test_season_change_during_oauth_prevents_wrong_boost_write(self):
        async def ensure_token():
            if self.harness.api_calls == READ:
                self.assertEqual(await self.harness.push(season_event("cooling")), 200)

        self.harness.oauth.async_ensure_token_valid.side_effect = ensure_token
        with self.assertRaises(self.harness.coordinator_module.HomeAssistantError):
            await self.run_command("boost", 30)
        self.assertEqual(self.state["season"], "cooling")
        self.assertEqual(self.harness.api_calls, READ)

    async def timed_temperature(self, season, prefix):
        await self.read_current(season=season)
        start = int(time())

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                deadline = fields[f"{prefix}_setpoint_end_time"]
                self.assertEqual(fields, {
                    f"{prefix}_setpoint_mode": "manual",
                    f"{prefix}_setpoint_temperature": 24.5,
                    f"{prefix}_setpoint_end_time": deadline,
                })
                self.assert_deadline(deadline, start, 47)
                await self.event(100, **fields)

        self.harness.http_hook = hook
        await self.run_command("temperature_duration", (24.5, 47))
        self.assertEqual(self.state["season"], season)
        self.assertEqual(self.state["room_mode"], "manual")
        self.assertEqual(self.state["target_temperature"], 24.5)
        self.assert_deadline(self.state["setpoint_end_time"], start, 47)
        self.assertEqual(self.harness.api_calls, READ + [WRITE])

    async def test_heating_manual_duration_is_confirmed_by_therm_fields(self):
        await self.timed_temperature("heating", "therm")

    async def test_cooling_manual_duration_is_confirmed_by_cooling_fields(self):
        await self.timed_temperature("cooling", "cooling")

    async def test_manual_target_match_does_not_confirm_wrong_expiry(self):
        previous_deadline = int(time()) + 1800
        await self.read_current(therm_setpoint_mode="manual", therm_setpoint_temperature=24.5,
                                therm_setpoint_end_time=previous_deadline)

        async def hook(endpoint, _):
            if endpoint == WRITE:
                await self.event(100, therm_setpoint_mode="manual", therm_setpoint_temperature=24.5,
                                 therm_setpoint_end_time=previous_deadline)

        self.harness.http_hook = hook
        with self.assertRaises(self.harness.coordinator_module.HomeAssistantError):
            await self.run_command("temperature_duration", (24.5, 60))
        self.assertEqual(self.state["setpoint_end_time"], previous_deadline)
        self.assertEqual(self.harness.api_calls, READ + [WRITE] + READ)

    async def test_cooling_boost_writes_and_confirms_only_cooling_fields(self):
        await self.read_current(season="cooling")
        started_at = int(time())

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                deadline = fields["cooling_setpoint_end_time"]
                self.assertEqual(fields, {
                    "cooling_setpoint_mode": "max", "cooling_setpoint_end_time": deadline,
                })
                self.assert_deadline(deadline, started_at, 60)
                await self.event(100, **self.observed_boost(fields))

        self.harness.http_hook = hook
        await self.run_command("boost", 60)
        self.assertEqual(self.state["season"], "cooling")
        self.assertEqual(self.state["room_mode"], "max")
        self.assertEqual(self.state["therm_setpoint_mode"], "home")
        self.assertIsNone(self.state["target_temperature"])
        self.assertEqual(self.harness.api_calls, READ + [WRITE])

    async def test_cooling_power_off_and_resume_preserve_global_schedule(self):
        await self.read_current(season="cooling")
        written = []

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                fields = self.written_room(kwargs)
                written.append(fields.copy())
                fields["cooling_setpoint_end_time"] = None
                self.harness.status["rooms"][0].update(fields)
                await self.event(100 + len(written), **fields)

        self.harness.http_hook = hook
        await self.run_command("power_off", None)
        self.assertEqual(self.state["room_mode"], "off")
        self.assertEqual(self.state["home_mode"], "schedule")
        await self.run_command("preset", "schedule")
        self.assertEqual(self.state["room_mode"], "home")
        self.assertEqual(self.state["home_mode"], "schedule")
        self.assertEqual(written, [
            {"cooling_setpoint_mode": "off"},
            {"cooling_setpoint_mode": "home"},
        ])
        self.assertEqual(self.harness.api_calls, (READ + [WRITE]) * 2)

    async def test_heating_power_off_remains_unsupported(self):
        with self.assertRaises(self.harness.coordinator_module.HomeAssistantError):
            await self.run_command("power_off", None)
        self.assertEqual(self.state["room_mode"], "home")
        self.assertNotIn(WRITE, self.harness.api_calls)

    async def test_cooling_automatic_uses_local_cooling_mode(self):
        await self.read_current(season="cooling", cooling_setpoint_mode="manual",
                                cooling_setpoint_end_time=int(time()) + 1800)

        async def hook(endpoint, kwargs):
            if endpoint == WRITE:
                self.assertEqual(self.written_room(kwargs), {"cooling_setpoint_mode": "home"})
                await self.event(100, cooling_setpoint_mode="home", cooling_setpoint_end_time=None)

        self.harness.http_hook = hook
        await self.run_command("preset", "schedule")
        self.assertEqual(self.state["season"], "cooling")
        self.assertEqual(self.state["room_mode"], "home")
        self.assertIsNone(self.state["setpoint_end_time"])
        self.assertEqual(self.harness.api_calls, READ + [WRITE])
