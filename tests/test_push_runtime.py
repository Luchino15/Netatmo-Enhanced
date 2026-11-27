"""Exercise Cloud Push concurrency through the adapter and coordinator."""

import asyncio
from copy import deepcopy
from statistics import median
from time import perf_counter
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from push_harness import HA_AVAILABLE, PushHarness, load_runtime
from push_payloads import (
    HOME, MODULE, ROOM, device_payload, heating_mode_event, room_event, schedule_event,
)


def manual_event(sequence, target):
    return room_event(sequence, therm_setpoint_mode="manual", therm_setpoint_temperature=target)


class PushRuntimeTests(IsolatedAsyncioTestCase):
    async def start(self, **kwargs):
        harness = await PushHarness.create(**kwargs)
        self.addAsyncCleanup(harness.stop)
        harness.api_calls.clear()
        harness.api_payloads.clear()
        return harness

    async def test_listener_sees_push_before_inflight_stale_fetch_returns(self):
        harness = await self.start()
        read_started = asyncio.Event()
        release_answer = asyncio.Event()
        old_status = deepcopy(harness.status)
        old_status["rooms"][0]["humidity"] = 58

        async def hook(endpoint, _):
            if endpoint == "homestatus":
                read_started.set()
                await release_answer.wait()
                return 200, {"status": "ok", "body": {"home": old_status}}, {}

        harness.http_hook = hook
        refresh = asyncio.create_task(harness.coordinator.async_refresh_for("periodic"))
        try:
            await asyncio.wait_for(read_started.wait(), 1)
            self.assertFalse(refresh.done())
            self.assertTrue(harness.coordinator.lock.locked())
            initial = harness.state
            update_count = len(harness.updates)
            self.assertEqual(await harness.push(manual_event(11, 24)), 200)
            self.assertEqual(len(harness.updates), update_count + 1)
            self.assertEqual(harness.updates[-1][MODULE]["target_temperature"], 24)
            self.assertEqual(initial[MODULE]["target_temperature"], 20)
            self.assertEqual(harness.api_calls, ["homesdata", "homestatus"])
        finally:
            release_answer.set()
            await refresh

        self.assertEqual(harness.state[MODULE]["target_temperature"], 24)
        self.assertEqual(harness.state[MODULE]["room_mode"], "manual")
        self.assertEqual(harness.state[MODULE]["humidity"], 58)

    async def test_api_overlay_cannot_make_actuators_contradict_each_other(self):
        harness = await self.start()
        read_started = asyncio.Event()
        release_answer = asyncio.Event()
        harness.status["modules"][0]["cooler_status"] = True
        harness.status["rooms"][0]["humidity"] = 58

        async def hook(endpoint, _):
            if endpoint == "homestatus":
                read_started.set()
                await release_answer.wait()

        harness.http_hook = hook
        refresh = asyncio.create_task(harness.coordinator.async_refresh_for("periodic"))
        try:
            await asyncio.wait_for(read_started.wait(), 1)
            await harness.push(device_payload(modules=[{
                "id": MODULE, "type": "BNS", "sequence_id": 11, "boiler_status": True,
            }]))
            self.assertIs(harness.state[MODULE]["boiler_status"], True)
            self.assertIs(harness.state[MODULE]["cooler_status"], False)
        finally:
            release_answer.set()
            await refresh
        self.assertIs(harness.state[MODULE]["boiler_status"], True)
        self.assertIs(harness.state[MODULE]["cooler_status"], False)
        self.assertEqual(harness.state[MODULE]["humidity"], 58)

    async def test_lagging_post_push_api_is_temporarily_ignored_then_can_correct(self):
        harness = await self.start()
        simulated_time = 10_000

        def clock():
            return simulated_time

        with patch.object(harness.coordinator_module, "monotonic", clock):
            await harness.push(manual_event(11, 23))
            simulated_time += 2
            await harness.coordinator.async_refresh_for("periodic")
            self.assertEqual(harness.state[MODULE]["target_temperature"], 23)
            self.assertEqual(harness.state[MODULE]["room_mode"], "manual")
            simulated_time += 45
            await harness.coordinator.async_refresh_for("periodic")
        self.assertEqual(harness.state[MODULE]["target_temperature"], 20)
        self.assertEqual(harness.state[MODULE]["room_mode"], "home")

    async def test_new_sequence_for_unchanged_value_still_protects_against_inflight_read(self):
        harness = await self.start()
        read_started = asyncio.Event()
        release_answer = asyncio.Event()
        simulated_time = 10_000

        def clock():
            return simulated_time

        async def hook(endpoint, _):
            if endpoint == "homestatus":
                read_started.set()
                await release_answer.wait()

        with patch.object(harness.coordinator_module, "monotonic", clock):
            await harness.push(room_event(11, humidity=53))
            simulated_time += 45
            harness.status["rooms"][0]["humidity"] = 54
            harness.http_hook = hook
            refresh = asyncio.create_task(harness.coordinator.async_refresh_for("periodic"))
            try:
                await asyncio.wait_for(read_started.wait(), 1)
                await harness.push(room_event(12, humidity=53))
            finally:
                release_answer.set()
                await refresh
            self.assertEqual(harness.state[MODULE]["humidity"], 53)
            harness.http_hook = None
            await harness.coordinator.async_refresh_for("periodic")
        self.assertEqual(harness.state[MODULE]["humidity"], 54)

    async def test_agreeing_api_reads_can_correct_despite_repeated_unchanged_pushes(self):
        harness = await self.start()
        simulated_time = 10_000

        def clock():
            return simulated_time

        with patch.object(harness.coordinator_module, "monotonic", clock):
            await harness.push(room_event(11, humidity=53))
            harness.status["rooms"][0]["humidity"] = 54
            simulated_time += 45

            async def hook(endpoint, _):
                if endpoint == "homestatus":
                    await harness.push(room_event(sequence, humidity=53))

            harness.http_hook = hook
            for sequence, expected in ((12, 53), (13, 54)):
                await harness.coordinator.async_refresh_for("periodic")
                self.assertEqual(harness.state[MODULE]["humidity"], expected)
                simulated_time += 60
        self.assertEqual(harness.api_calls, ["homesdata", "homestatus"] * 2)

    async def test_push_received_while_command_awaits_response_confirms_after_success(self):
        harness = await self.start()
        write_started = asyncio.Event()
        release_answer = asyncio.Event()

        async def hook(endpoint, _):
            if endpoint == "setstate":
                write_started.set()
                await release_answer.wait()

        harness.http_hook = hook
        pending = asyncio.create_task(harness.coordinator.async_command(MODULE, "temperature", 24))
        try:
            await asyncio.wait_for(write_started.wait(), 1)
            self.assertTrue(harness.coordinator.command_lock.locked())
            self.assertEqual(await harness.push(manual_event(11, 24)), 200)
            self.assertEqual(harness.state[MODULE]["target_temperature"], 24)
            self.assertFalse(pending.done())
        finally:
            release_answer.set()
            await pending
        self.assertEqual(harness.api_calls, ["homesdata", "homestatus", "setstate"])
        self.assertEqual(harness.api_payloads[-1]["json"]["home"]["rooms"], [{
            "id": ROOM, "therm_setpoint_mode": "manual", "therm_setpoint_temperature": 24,
        }])

    async def test_push_received_after_command_response_confirms_without_read(self):
        harness = await self.start()
        write_returned = asyncio.Event()
        write_seen = asyncio.Event()

        async def hook(endpoint, _):
            if endpoint == "setstate":
                write_seen.set()
                asyncio.get_running_loop().call_soon(write_returned.set)

        harness.http_hook = hook
        pending = asyncio.create_task(harness.coordinator.async_command(MODULE, "temperature", 24))
        try:
            await asyncio.wait_for(write_seen.wait(), 1)
            await asyncio.wait_for(write_returned.wait(), 1)
            self.assertFalse(pending.done())
            await harness.push(manual_event(11, 24))
            await asyncio.wait_for(pending, 1)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(harness.api_calls, ["homesdata", "homestatus", "setstate"])

    async def test_missing_command_push_uses_one_authoritative_fallback(self):
        harness = await self.start()

        async def hook(endpoint, _):
            if endpoint == "setstate":
                harness.status["rooms"][0].update(
                    therm_setpoint_mode="manual", therm_setpoint_temperature=24,
                )

        harness.http_hook = hook
        with patch.object(harness.coordinator_module, "COMMAND_PUSH_WAIT_SECONDS", 0.01):
            await asyncio.wait_for(harness.coordinator.async_command(MODULE, "temperature", 24), 1)
        self.assertEqual(harness.state[MODULE]["target_temperature"], 24)
        self.assertEqual(harness.api_calls, [
            "homesdata", "homestatus", "setstate", "homesdata", "homestatus",
        ])

    async def test_mismatching_external_push_does_not_confirm_command(self):
        harness = await self.start()

        async def hook(endpoint, _):
            if endpoint == "setstate":
                await harness.push(manual_event(11, 22))

        harness.http_hook = hook
        with (patch.object(harness.coordinator_module, "COMMAND_PUSH_WAIT_SECONDS", 0.01),
              self.assertRaises(harness.coordinator_module.HomeAssistantError)):
            await asyncio.wait_for(harness.coordinator.async_command(MODULE, "temperature", 24), 1)
        self.assertEqual(harness.state[MODULE]["target_temperature"], 22)
        self.assertEqual(harness.api_calls.count("setstate"), 1)

    async def test_matching_external_push_before_http_write_is_not_command_confirmation(self):
        harness = await self.start()
        matching_event_sent = False

        async def ensure_token():
            nonlocal matching_event_sent
            if harness.api_calls == ["homesdata", "homestatus"] and not matching_event_sent:
                matching_event_sent = True
                await harness.push(manual_event(11, 24))

        harness.oauth.async_ensure_token_valid.side_effect = ensure_token
        with (patch.object(harness.coordinator_module, "COMMAND_PUSH_WAIT_SECONDS", 0.01),
              self.assertRaises(harness.coordinator_module.HomeAssistantError)):
            await asyncio.wait_for(harness.coordinator.async_command(MODULE, "temperature", 24), 1)
        self.assertTrue(matching_event_sent)
        self.assertEqual(harness.state[MODULE]["target_temperature"], 24)
        self.assertEqual(harness.api_calls, [
            "homesdata", "homestatus", "setstate", "homesdata", "homestatus",
        ])

    async def test_failed_command_still_publishes_real_push_but_reports_write_error(self):
        harness = await self.start()

        async def hook(endpoint, _):
            if endpoint == "setstate":
                await harness.push(manual_event(11, 24))
                return 503, None, {}

        harness.http_hook = hook
        with self.assertRaises(harness.coordinator_module.HomeAssistantError):
            await asyncio.wait_for(harness.coordinator.async_command(MODULE, "temperature", 24), 1)
        self.assertEqual(harness.state[MODULE]["target_temperature"], 24)
        self.assertEqual(harness.api_calls, ["homesdata", "homestatus", "setstate"])

    async def test_commands_serialize_and_automatic_keeps_setstate_home(self):
        harness = await self.start()
        set_manual = asyncio.Event()
        allow_manual = asyncio.Event()

        async def hook(endpoint, kwargs):
            if endpoint != "setstate":
                return None
            room = kwargs["json"]["home"]["rooms"][0]
            if room["therm_setpoint_mode"] == "manual":
                set_manual.set()
                await allow_manual.wait()
                await harness.push(manual_event(11, 24))
            else:
                await harness.push(room_event(12, therm_setpoint_mode="home"))

        harness.http_hook = hook
        first = asyncio.create_task(harness.coordinator.async_command(MODULE, "temperature", 24))
        second = None
        try:
            await asyncio.wait_for(set_manual.wait(), 1)
            second = asyncio.create_task(harness.coordinator.async_command(MODULE, "preset", "schedule"))
            await asyncio.sleep(0)
            self.assertEqual(harness.api_calls, ["homesdata", "homestatus", "setstate"])
            allow_manual.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
        finally:
            allow_manual.set()
            for task in (first, second):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(harness.api_calls.count("setstate"), 2)
        self.assertNotIn("setthermmode", harness.api_calls)
        self.assertEqual(harness.api_payloads[-1]["json"]["home"]["rooms"], [{
            "id": ROOM, "therm_setpoint_mode": "home",
        }])
        self.assertEqual(harness.state[MODULE]["room_mode"], "home")

    async def test_global_return_reads_intermediate_state_before_clearing_manual_override(self):
        harness = await self.start()
        harness.home["therm_mode"] = "away"
        harness.status["rooms"][0].update(therm_setpoint_mode="manual", therm_setpoint_temperature=22)
        await harness.coordinator.async_refresh_for("periodic")
        harness.api_calls.clear()

        async def hook(endpoint, _):
            if endpoint == "setthermmode":
                harness.home["therm_mode"] = "schedule"
                await harness.push(heating_mode_event("schedule"))
            elif endpoint == "setstate":
                await harness.push(room_event(11, therm_setpoint_mode="home"))

        harness.http_hook = hook
        with patch.object(harness.coordinator_module, "COMMAND_PUSH_WAIT_SECONDS", 0.01):
            await asyncio.wait_for(harness.coordinator.async_command(MODULE, "preset", "schedule"), 1)
        self.assertEqual(harness.api_calls, [
            "homesdata", "homestatus", "setthermmode", "homesdata", "homestatus", "setstate",
        ])
        self.assertEqual(harness.state[MODULE]["room_mode"], "home")
        self.assertEqual(harness.state[MODULE]["home_mode"], "schedule")

    async def test_new_external_override_during_oauth_aborts_second_stage_of_global_return(self):
        harness = await self.start()
        harness.home["therm_mode"] = "away"
        harness.status["rooms"][0].update(therm_setpoint_mode="manual", therm_setpoint_temperature=22)
        await harness.coordinator.async_refresh_for("periodic")
        harness.api_calls.clear()
        first_stage = ["homesdata", "homestatus", "setthermmode", "homesdata", "homestatus"]
        external = asyncio.Event()

        async def hook(endpoint, _):
            if endpoint == "setthermmode":
                harness.home["therm_mode"] = "schedule"
                await harness.push(heating_mode_event("schedule"))

        async def ensure_token():
            if harness.api_calls == first_stage:
                self.assertTrue(harness.coordinator.lock.locked())
                await harness.push(manual_event(11, 25))
                external.set()

        harness.http_hook = hook
        harness.oauth.async_ensure_token_valid.side_effect = ensure_token
        with (patch.object(harness.coordinator_module, "COMMAND_PUSH_WAIT_SECONDS", 0.01),
              self.assertRaises(harness.coordinator_module.HomeAssistantError)):
            await asyncio.wait_for(harness.coordinator.async_command(MODULE, "preset", "schedule"), 1)
        self.assertTrue(external.is_set())
        self.assertEqual(harness.api_calls, first_stage)
        self.assertEqual(harness.state[MODULE]["room_mode"], "manual")
        self.assertEqual(harness.state[MODULE]["target_temperature"], 25)

    async def test_api_failures_preserve_recent_push_and_429_gates_physical_requests(self):
        harness = await self.start()
        temporary_code = 503

        async def hook(endpoint, _):
            if endpoint == "homesdata":
                return temporary_code, None, {"Retry-After": "1000"}

        await harness.push(manual_event(11, 24))
        harness.http_hook = hook
        await harness.coordinator.async_refresh_for("periodic")
        self.assertEqual(harness.state[MODULE]["target_temperature"], 24)
        temporary_code = 429
        await harness.coordinator.async_refresh_for("periodic")
        self.assertEqual(harness.state[MODULE]["target_temperature"], 24)
        await harness.coordinator.async_refresh_for("periodic")
        self.assertEqual(harness.api_calls, ["homesdata", "homesdata"])
        self.assertGreater(harness.coordinator.guard["until"], harness.coordinator_module.monotonic())
        rows = harness.coordinator.metrics.snapshot()["http_requests"]
        periodic = next(row for row in rows if row["source"] == "periodic")
        self.assertEqual(periodic["status_codes"], {"503": 1, "429": 1})

    async def test_prolonged_api_failure_marks_unavailable_but_keeps_last_state(self):
        simulated_time = 10_000

        def clock():
            return simulated_time

        harness = await self.start()
        with patch.object(harness.coordinator_module, "monotonic", clock):
            await harness.coordinator.async_refresh_for("periodic")
            await harness.push(manual_event(11, 24))

            async def hook(endpoint, _):
                if endpoint == "homesdata":
                    raise TimeoutError()

            harness.http_hook = hook
            await harness.coordinator.async_refresh_for("periodic")
            self.assertTrue(harness.coordinator.last_update_success)
            simulated_time += 25 * 60
            await harness.coordinator.async_refresh_for("periodic")
            self.assertFalse(harness.coordinator.last_update_success)
            self.assertEqual(harness.state[MODULE]["target_temperature"], 24)
            harness.http_hook = None
            await harness.coordinator.async_refresh_for("periodic")
        self.assertTrue(harness.coordinator.last_update_success)
        self.assertEqual(harness.state[MODULE]["target_temperature"], 20)

    async def test_periodic_reads_keep_running_with_frequent_pushes_and_recover_outage(self):
        coordinator_module, _ = load_runtime(HA_AVAILABLE)
        self.assertEqual(coordinator_module.POLL_SECONDS, 600)
        poll_patch = patch.object(coordinator_module, "POLL_SECONDS", 0.09)
        poll_patch.start()
        self.addCleanup(poll_patch.stop)
        harness = await self.start()
        read_while_pushing = asyncio.Event()

        async def hook(endpoint, _):
            if endpoint == "homestatus":
                read_while_pushing.set()

        harness.http_hook = hook
        for sequence in range(1, 150):
            await harness.push(room_event(sequence, humidity=40 + sequence % 10))
            if read_while_pushing.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(read_while_pushing.is_set())
        self.assertEqual(harness.api_calls[:2], ["homesdata", "homestatus"])
        await harness.adapter.stop()
        read_while_pushing.clear()
        harness.status["rooms"][0]["therm_measured_temperature"] = 18.9
        await asyncio.wait_for(read_while_pushing.wait(), 0.5)
        self.assertEqual(harness.state[MODULE]["current_temperature"], 18.9)
        self.assertGreaterEqual(harness.oauth.async_ensure_token_valid.await_count, 5)

    async def test_topology_and_unknown_schedule_share_one_read_at_60_second_limit(self):
        harness = await self.start()
        fallback_wait = asyncio.Event()
        release_wait = asyncio.Event()
        simulated_time = harness.coordinator.guard["refresh_started"]
        delays = []

        def clock():
            return simulated_time

        async def sleep(delay):
            delays.append(delay)
            fallback_wait.set()
            await release_wait.wait()

        clocked_asyncio = SimpleNamespace(
            sleep=sleep, timeout=asyncio.timeout, CancelledError=asyncio.CancelledError,
        )
        topology_event = {
            "push_type": "topology_changed", "user_id": "demo-user", "home_id": HOME,
        }
        with (patch.object(harness.adapter_module, "asyncio", clocked_asyncio),
              patch.object(harness.adapter_module, "monotonic", clock),
              patch.object(harness.coordinator_module, "monotonic", clock)):
            self.assertEqual(await harness.push(topology_event), 200)
            await asyncio.wait_for(fallback_wait.wait(), 1)
            self.assertEqual(await harness.push(schedule_event("new-test-schedule")), 200)
            self.assertEqual(delays, [60])
            self.assertEqual(harness.api_calls, [])
            self.assertTrue(harness.adapter.diagnostics()["refresh_pending"])
            simulated_time += 60
            release_wait.set()
            await asyncio.wait_for(harness.adapter._refresh_task, 1)
        self.assertEqual(harness.api_calls, ["homesdata", "homestatus"])
        diagnostics = harness.adapter.diagnostics()
        self.assertFalse(diagnostics["refresh_pending"])
        self.assertEqual(diagnostics["refresh_success"], 1)
        self.assertEqual(diagnostics["reconciliation_reasons"], {
            "topology_changed": 1, "unknown_schedule": 1,
        })

    async def test_complete_push_batch_does_not_read_api_and_meets_offline_latency(self):
        harness = await self.start()
        timings = []
        for sequence in range(1, 101):
            raw_event = room_event(sequence, humidity=40 + sequence % 10)
            started = perf_counter()
            self.assertEqual(await harness.push(raw_event), 200)
            timings.append((perf_counter() - started) * 1000)
            self.assertEqual(harness.state[MODULE]["humidity"], 40 + sequence % 10)
        self.assertEqual(len(harness.updates), 100)
        self.assertEqual(harness.api_calls, [])
        publication = [event["receipt_to_publication_ms"]
                       for event in harness.adapter.diagnostics()["recent_events"]]
        self.assertEqual(len(publication), 100)
        p95 = sorted(publication)[94]
        self.assertLess(p95, 100)
        print(
            f"\nCloud Push offline, 100 receipts, real HA={harness.real_ha}, 0 HTTP requests. "
            f"Receipt to publication: median {median(publication):.3f} ms, p95 {p95:.3f} ms, "
            f"max {max(publication):.3f} ms. "
            f"Fixture through response: median {median(timings):.3f} ms."
        )
