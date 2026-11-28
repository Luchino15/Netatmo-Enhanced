"""Offline tests for partial Netatmo webhooks and the projected entity data."""

from copy import deepcopy
import importlib
import unittest
from unittest.mock import Mock

from push_payloads import (
    HOME, MODULE, ROOM, SUMMER, WINTER, WINTER_ALT, device_payload,
    heating_mode_event, home_payload, module_event, room_event, schedule_event, season_event,
)
from test_webhook import SECRET, core, package, signed

push = importlib.import_module(f"{package.__name__}.cloud_push")
model = importlib.import_module(f"{package.__name__}.model")


def api_state(home_id=HOME, room_id=ROOM, module_id=MODULE):
    room = {"id": room_id, "name": "Demo room"}
    module = {"id": module_id, "room_id": room_id, "type": "BNS"}
    topology = {
        "id": home_id, "rooms": [room], "modules": [module],
        "temperature_control_mode": "heating", "therm_mode": "schedule",
        "cooling_mode": "schedule", "schedules": [
            {"type": "therm", "id": WINTER, "name": "Winter", "selected": True},
            {"type": "therm", "id": WINTER_ALT, "name": "Winter alternate"},
            {"type": "cooling", "id": SUMMER, "name": "Summer", "selected": True},
        ],
    }
    status = {
        "rooms": [{
            "id": room_id, "therm_measured_temperature": 21.5,
            "therm_setpoint_temperature": 18, "therm_setpoint_mode": "home",
            "cooling_setpoint_temperature": 26, "cooling_setpoint_mode": "home", "humidity": 51,
        }],
        "modules": [{"id": module_id, "boiler_status": False, "cooler_status": False,
                     "reachable": True}],
    }
    return model.snapshot(topology, status, room, module)


class PushProjectionTests(unittest.TestCase):
    def setUp(self):
        self.state = {MODULE: api_state()}
        self.clock = Mock(return_value=1000)
        self.push = push.PushState(clock=self.clock)

    def apply(self, payload):
        before = deepcopy(self.state)
        result = self.push.apply(self.state, payload)
        self.assertEqual(self.state, before, "Published snapshot must stay immutable")
        self.state = result.data
        return result

    def assert_values(self, **expected):
        actual = self.state[MODULE]
        self.assertEqual({key: actual[key] for key in expected}, expected)

    def test_target_ambient_humidity_and_manual_expiry(self):
        expiry = 2_000_000_000
        result = self.apply(room_event(100, therm_measured_temperature=22,
                                       therm_setpoint_mode="manual", therm_setpoint_temperature=23.5,
                                       therm_setpoint_end_time=expiry))
        self.assertTrue(result.changed)
        self.assertFalse(result.reasons)
        self.assert_values(current_temperature=22, target_temperature=23.5, mode="manual",
                           home_mode="schedule", setpoint_end_time=expiry)

        result = self.apply(room_event(100, humidity=52))
        self.assertTrue(result.partial)
        self.assertFalse(result.reasons)
        self.assert_values(current_temperature=22, target_temperature=23.5, humidity=52,
                           mode="manual", setpoint_end_time=expiry)

        self.apply(room_event(101, therm_measured_temperature=22.7))
        self.assert_values(current_temperature=22.7, target_temperature=23.5,
                           humidity=52, setpoint_end_time=expiry)

        result = self.apply(room_event(102, therm_setpoint_mode="home",
                                       therm_setpoint_temperature=19, therm_setpoint_end_time=None))
        self.assertFalse(result.reasons)
        self.assert_values(mode="home", room_mode="home", home_mode="schedule",
                           target_temperature=19, setpoint_end_time=None)

    def test_heating_boost_replay_changes_deadline_and_resumes_real_target(self):
        first_deadline = 2_100_000_000
        activation = room_event(
            110, therm_setpoint_mode="max", therm_setpoint_temperature=None,
            therm_setpoint_end_time=first_deadline, therm_measured_temperature=20,
        )
        start = self.apply(activation)
        self.assertFalse(start.reasons)
        self.assertEqual(start.accepted[MODULE], {
            "therm_setpoint_mode", "therm_setpoint_temperature", "therm_setpoint_end_time",
            "therm_end_time_is_boost", "current_temperature",
        })
        self.assert_values(room_mode="max", mode="max", home_mode="schedule",
                           target_temperature=None, setpoint_end_time=first_deadline,
                           boiler_status=False, current_temperature=20, humidity=51)
        self.assertFalse(self.apply(module_event(110, True, False)).reasons)
        self.assert_values(boiler_status=True)

        for sequence, minutes in enumerate((60, 90), start=111):
            with self.subTest(minutes=minutes):
                deadline = first_deadline + (minutes - 30) * 60
                duration = self.apply(room_event(
                    sequence, therm_setpoint_mode="max", therm_setpoint_temperature=None,
                    therm_setpoint_end_time=deadline,
                ))
                self.assertFalse(duration.reasons)
                self.assert_values(mode="max", target_temperature=None, setpoint_end_time=deadline)

        # The observed stop and the fixture modelling natural expiration have
        # the same known shape. Only a confirmed room event restores the setpoint.
        for termination in (113, 115):
            with self.subTest(termination=termination):
                stop = self.apply(room_event(
                    termination, therm_setpoint_mode="home", therm_setpoint_temperature=19,
                    therm_setpoint_end_time=None,
                ))
                self.assertFalse(stop.reasons)
                self.assert_values(room_mode="home", mode="home", home_mode="schedule",
                                   target_temperature=19, setpoint_end_time=None)
                self.assertFalse(self.apply(module_event(termination, False, False)).reasons)
                self.assert_values(boiler_status=False)
                if termination == 113:
                    self.apply(room_event(
                        114, therm_setpoint_mode="max", therm_setpoint_temperature=None,
                        therm_setpoint_end_time=first_deadline + 7_200,
                    ))

    def test_boost_fragments_preserve_missing_fields_and_pair_explicit_nulls(self):
        deadline = 2_100_000_000
        result = self.apply(room_event(120, therm_setpoint_temperature=None))
        self.assertIn("boost_context_missing", result.reasons)
        self.assert_values(room_mode="home", target_temperature=18)
        result = self.apply(room_event(
            120, therm_setpoint_mode="max", therm_setpoint_end_time=deadline,
        ))
        self.assertFalse(result.reasons)
        self.assertIn("therm_setpoint_temperature", result.accepted[MODULE])
        self.assert_values(room_mode="max", target_temperature=None, setpoint_end_time=deadline)

        self.apply(room_event(120, humidity=54))
        self.assert_values(mode="max", humidity=54, setpoint_end_time=deadline)
        result = self.apply(room_event(121, therm_setpoint_end_time=None))
        self.assertIn("boost_details_missing", result.reasons)
        self.assert_values(mode="max", setpoint_end_time=deadline)
        result = self.apply(room_event(
            121, therm_setpoint_mode="home", therm_setpoint_temperature=20,
        ))
        self.assertFalse(result.reasons)
        self.assertIn("therm_setpoint_end_time", result.accepted[MODULE])
        self.assert_values(mode="home", humidity=54, target_temperature=20, setpoint_end_time=None)

    def test_mode_only_boost_displays_no_prior_target_and_accepts_same_sequence_fragments(self):
        self.apply(room_event(129, therm_setpoint_mode="manual",
                              therm_setpoint_end_time=2_099_999_800))
        activation = self.apply(room_event(130, therm_setpoint_mode="max"))
        self.assertIn("boost_details_missing", activation.reasons)
        self.assert_values(mode="max", therm_setpoint_temperature=18, target_temperature=None,
                           therm_setpoint_end_time=2_099_999_800, setpoint_end_time=None)
        without_order = self.apply(room_event(therm_setpoint_end_time=2_099_999_900))
        self.assertFalse(without_order.changed)
        self.assertIn("unsequenced_reversal", without_order.reasons)
        details = self.apply(room_event(
            130, therm_setpoint_temperature=None, therm_setpoint_end_time=2_100_000_000,
        ))
        self.assertFalse(details.reasons)
        self.assert_values(mode="max", therm_setpoint_temperature=None,
                           target_temperature=None, setpoint_end_time=2_100_000_000)

    def test_deadline_fragment_from_before_same_sequence_boost_mode_is_associated(self):
        deadline = 2_100_000_000
        self.apply(room_event(135, therm_setpoint_end_time=deadline))
        result = self.apply(room_event(
            135, therm_setpoint_mode="max", therm_setpoint_temperature=None,
        ))
        self.assertFalse(result.reasons)
        self.assertIn("therm_setpoint_end_time", result.accepted[MODULE])
        self.assert_values(mode="max", target_temperature=None, setpoint_end_time=deadline)

    def test_boost_out_of_order_and_duplicate_packets_do_not_restore_stale_state(self):
        deadline = 2_100_000_000
        activation = room_event(
            140, therm_setpoint_mode="max", therm_setpoint_temperature=None,
            therm_setpoint_end_time=deadline,
        )
        self.apply(activation)
        duplicate = self.apply(activation)
        self.assertFalse(duplicate.changed)
        self.assertEqual(duplicate.outcomes, {"duplicate"})
        self.apply(room_event(
            142, therm_setpoint_mode="home", therm_setpoint_temperature=20,
        ))
        self.assert_values(therm_setpoint_end_time=deadline, setpoint_end_time=None)
        stale = self.apply(room_event(141, therm_setpoint_end_time=deadline + 1_800))
        self.assertIn("obsolete", stale.outcomes)
        self.assertFalse(stale.changed)
        stale = self.apply(activation)
        self.assertFalse(stale.changed)
        self.assert_values(mode="home", target_temperature=20)
        self.assertFalse(self.apply(room_event(142, therm_setpoint_end_time=None)).reasons)
        self.assert_values(mode="home", target_temperature=20, setpoint_end_time=None)

    def test_cooling_boost_and_off_keep_null_validation_contextual(self):
        self.apply(season_event("cooling"))
        invalid = self.apply(room_event(150, cooling_setpoint_mode="off",
                                        cooling_setpoint_temperature=None))
        self.assertIn("boost_context_missing", invalid.reasons)
        self.assert_values(mode="off", target_temperature=26)

        valid = self.apply(room_event(
            151, cooling_setpoint_mode="max", cooling_setpoint_temperature=None,
            cooling_setpoint_end_time=2_100_000_000,
        ))
        self.assertFalse(valid.reasons)
        self.assert_values(mode="max", target_temperature=None, setpoint_end_time=2_100_000_000)

        invalid = self.apply(room_event(152, therm_setpoint_temperature=None))
        self.assertIn("boost_context_missing", invalid.reasons)
        self.assert_values(mode="max", therm_setpoint_temperature=18)
        stop = self.apply(room_event(
            153, cooling_setpoint_mode="home", cooling_setpoint_temperature=25,
            cooling_setpoint_end_time=None,
        ))
        self.assertFalse(stop.reasons)
        self.assert_values(mode="home", target_temperature=25, setpoint_end_time=None)

    def test_api_boost_with_old_numeric_target_never_exposes_a_boost_setpoint(self):
        for season, field in (("heating", "therm"), ("cooling", "cooling")):
            with self.subTest(season=season):
                state = model.project({
                    **api_state(), "season": season, f"{field}_setpoint_mode": "max",
                    f"{field}_setpoint_end_time": 2_100_000_000,
                    f"{field}_end_time_is_boost": True,
                })
                self.assertEqual(state["mode"], "max")
                self.assertIsNone(state["target_temperature"])
                self.assertEqual(state["setpoint_end_time"], 2_100_000_000)

    def test_away_and_frost_guard_keep_global_and_local_modes_separate(self):
        self.assertFalse(self.apply(heating_mode_event("away")).reasons)
        self.apply(room_event(100, therm_setpoint_mode="home", therm_setpoint_temperature=14))
        self.assert_values(home_mode="away", room_mode="home", mode="away", target_temperature=14)

        result = self.apply(room_event(101, therm_setpoint_mode="hg", therm_setpoint_temperature=8))
        self.assertTrue(result.changed)
        self.assertTrue(result.reasons)
        self.assert_values(home_mode="away", room_mode="hg", mode="hg", target_temperature=8)

    def test_known_schedule_changes_both_directions_and_unknown_schedule_recovers(self):
        self.assertFalse(self.apply(schedule_event(WINTER_ALT, nested=True)).reasons)
        self.assert_values(active_schedule_id=WINTER_ALT, active_schedule="Winter alternate")
        self.assertFalse(self.apply(schedule_event(WINTER)).reasons)
        self.assert_values(active_schedule_id=WINTER, active_schedule="Winter")

        for payload in (schedule_event("unknown-schedule"), home_payload(event_type="schedule")):
            with self.subTest(payload=payload):
                result = self.apply(payload)
                self.assertTrue(result.reasons)
                self.assert_values(active_schedule_id=WINTER, active_schedule="Winter")

    def test_both_season_shapes_project_cached_setpoints_and_schedules(self):
        self.apply(room_event(200, cooling_setpoint_mode="manual", cooling_setpoint_temperature=25))
        self.assert_values(season="heating", room_mode="home", target_temperature=18)

        to_cooling = season_event("cooling")
        self.assertFalse(self.apply(to_cooling).reasons)
        self.assert_values(season="cooling", room_mode="manual", target_temperature=25,
                           active_schedule_id=SUMMER, active_schedule="Summer")
        self.assertFalse(self.apply(season_event("heating", nested=True)).reasons)
        self.assert_values(season="heating", room_mode="home", target_temperature=18,
                           active_schedule_id=WINTER)

        repeat = self.apply(to_cooling)
        self.assertNotIn("duplicate", repeat.outcomes)
        self.assertTrue(repeat.reasons or repeat.changed)

        self.clock.return_value += 61
        self.assertFalse(self.apply(to_cooling).reasons)
        self.assert_values(season="cooling", target_temperature=25, active_schedule_id=SUMMER)

    def test_module_flags_and_same_sequence_for_distinct_targets(self):
        self.apply(room_event(300, therm_setpoint_mode="home", therm_setpoint_temperature=24.5))
        self.assertFalse(self.apply(module_event(300, True, False)).reasons)
        self.assert_values(boiler_status=True, cooler_status=False, target_temperature=24.5)
        self.assertFalse(self.apply(module_event(301, False, False)).reasons)
        self.assert_values(boiler_status=False, cooler_status=False)

        self.apply(season_event("cooling", nested=True))
        self.apply(module_event(302, False, True))
        self.assert_values(boiler_status=False, cooler_status=True)
        self.apply(module_event(303, False, False))
        self.assert_values(boiler_status=False, cooler_status=False)

    def test_contradictory_and_non_boolean_actuator_pairs_require_api(self):
        self.apply(module_event(310, True, False))
        for sequence, boiler, cooler in ((311, True, True), (312, 0, True), (313, None, True)):
            with self.subTest(boiler=boiler, cooler=cooler):
                self.assertTrue(self.apply(module_event(sequence, boiler, cooler)).reasons)
                self.assert_values(boiler_status=True, cooler_status=False)

    def test_duplicates_conflicts_and_real_return_at_new_sequence(self):
        first = room_event(400, humidity=56)
        self.assertFalse(self.apply(first).reasons)
        duplicate = self.apply(first)
        self.assertFalse(duplicate.changed)
        self.assertIn("duplicate", duplicate.outcomes)

        conflict = self.apply(room_event(400, humidity=58))
        self.assertTrue(conflict.reasons)
        self.assert_values(humidity=56)

        self.apply(room_event(410, humidity=57))
        self.assertFalse(self.apply(room_event(420, humidity=56)).reasons)
        self.assert_values(humidity=56)

    def test_same_value_at_new_sequence_is_unchanged_and_still_orders_the_field(self):
        self.apply(room_event(410, humidity=55))
        result = self.apply(room_event(430, humidity=55))
        self.assertFalse(result.changed)
        self.assertIn("unchanged", result.outcomes)
        self.assertNotIn("duplicate", result.outcomes)
        self.apply(room_event(420, humidity=59))
        self.assert_values(humidity=55)

    def test_older_fields_do_not_erase_newer_fields(self):
        self.apply(room_event(530, humidity=56))
        result = self.apply(room_event(520, humidity=53, therm_measured_temperature=23.4))
        self.assertIn("obsolete", result.outcomes)
        self.assert_values(humidity=56, current_temperature=23.4)

    def test_first_unsequenced_update_then_uncertain_reversal(self):
        first = room_event(humidity=54)
        self.apply(first)
        self.assert_values(humidity=54)
        self.apply(room_event(humidity=58))
        repeated_value = self.apply(first)
        self.assertNotIn("duplicate", repeated_value.outcomes)
        self.assertTrue(repeated_value.reasons)
        self.assert_values(humidity=58)

    def test_unsequenced_noop_can_later_be_an_ambiguous_retransmission(self):
        already_current = room_event(humidity=51)
        unchanged = self.apply(already_current)
        self.assertIn("unchanged", unchanged.outcomes)
        self.apply(room_event(humidity=53))
        repeated_value = self.apply(already_current)
        self.assertFalse(repeated_value.changed)
        self.assertIn("unsequenced_reversal", repeated_value.reasons)
        self.assert_values(humidity=53)

    def test_sequence_reset_requires_authoritative_baseline_and_allows_new_epoch(self):
        self.apply(room_event(10_000, humidity=55))
        self.assertTrue(self.apply(room_event(2, humidity=56)).reasons)
        self.assert_values(humidity=55)

        self.state = {MODULE: {**api_state(), "humidity": 56}}
        self.push.reconciled(self.state)
        self.apply(room_event(3, humidity=57))
        self.assert_values(humidity=57)

    def test_restart_accepts_new_sequences_on_fresh_api_baseline(self):
        self.apply(room_event(10_000, humidity=55))
        self.push = push.PushState()
        self.state = {MODULE: api_state()}
        result = self.apply(room_event(2, humidity=56))
        self.assertFalse(result.reasons)
        self.assert_values(humidity=56)

    def test_invalid_missing_null_and_unknown_fields_remain_distinct(self):
        self.apply(room_event(610, therm_setpoint_mode="manual", therm_setpoint_temperature=24,
                              therm_setpoint_end_time=2_000_000_000))
        result = self.apply(room_event(611, humidity=54, therm_setpoint_temperature="24.5",
                                       therm_measured_temperature=None, future_value="ignore"))
        self.assertTrue(result.changed)
        self.assertTrue(result.reasons)
        self.assert_values(humidity=54, target_temperature=24, current_temperature=21.5,
                           setpoint_end_time=2_000_000_000)
        self.apply(room_event(612, therm_setpoint_end_time=None))
        self.assert_values(setpoint_end_time=None)

        for unexpected in (True, -1, 101, float("inf"), None):
            with self.subTest(humidity=unexpected):
                self.assertTrue(self.apply(room_event(613, humidity=unexpected)).reasons)
                self.assert_values(humidity=54)

        unknown = self.apply(room_event(614, future_value=52))
        self.assertFalse(unknown.changed)
        self.assertIn("unrecognized", unknown.outcomes)

    def test_multiple_rooms_modules_and_homes_are_isolated(self):
        second = "demo-module-second"
        other_home = "other-home"
        other_module = "other-module"
        self.state[second] = api_state(room_id="second-room", module_id=second)
        self.state[other_module] = api_state(other_home, ROOM, other_module)

        self.apply(device_payload(rooms=[
            {"id": ROOM, "sequence_id": 700, "humidity": 55},
            {"id": "second-room", "sequence_id": 700, "humidity": 56},
        ], modules=[
            {"id": MODULE, "sequence_id": 700, "type": "BNS", "boiler_status": True,
             "cooler_status": False},
            {"id": other_module, "sequence_id": 700, "type": "BNS", "boiler_status": True,
             "cooler_status": False},
        ]))
        self.assert_values(humidity=55, boiler_status=True)
        self.assertEqual(self.state[second]["humidity"], 56)
        self.assertEqual(self.state[other_module]["humidity"], 51)
        self.assertFalse(self.state[other_module]["boiler_status"])

        self.apply(schedule_event(WINTER_ALT))
        self.assertEqual(self.state[second]["active_schedule_id"], WINTER_ALT)
        self.assertEqual(self.state[other_module]["active_schedule_id"], WINTER)

    def test_conflicting_home_identifiers_do_not_publish(self):
        payload = season_event("cooling", nested=True)
        payload["home_id"] = "unknown-home"
        self.assertFalse(self.apply(payload).changed)
        self.assert_values(season="heating")

    def test_conflicting_schedule_copies_preserve_the_existing_schedule(self):
        payload = home_payload(event_type="schedule", schedule_id=WINTER_ALT,
                               home={"schedule_id": WINTER, "therm_mode": "away"})
        result = self.apply(payload)
        self.assertTrue(result.reasons)
        self.assert_values(home_mode="away", active_schedule_id=WINTER)

    def test_wrong_types_in_sequence_cannot_publish(self):
        for sequence in (-1, True, "7", 7.0):
            with self.subTest(sequence=sequence):
                result = self.apply(room_event(sequence, humidity=54))
                self.assertFalse(result.changed)
                self.assertTrue(result.reasons)

    def test_preriscaldo_uses_reported_target_and_physical_activity(self):
        self.apply(room_event(800, therm_setpoint_mode="home", therm_setpoint_temperature=24.5,
                              therm_measured_temperature=20))
        self.apply(module_event(800, True, False))
        self.assert_values(target_temperature=24.5, current_temperature=20, boiler_status=True)

        self.apply(room_event(801, therm_setpoint_mode="manual", therm_setpoint_temperature=24))
        self.apply(room_event(802, therm_setpoint_mode="home", therm_setpoint_temperature=17))
        self.assert_values(target_temperature=17, current_temperature=20, boiler_status=True)


class RoutedValidationTests(unittest.TestCase):
    def test_signed_json_with_duplicate_id_key_is_rejected(self):
        signal = Mock()
        receiver = core.Receiver(SECRET, lambda _: True, signal, signal_push=signal)
        raw = (b'{"push_type":"device_event","user_id":"demo-user",'
               b'"home_id":"demo-home","home_id":"conflicting-home"}')
        self.assertEqual(receiver.accept(raw, signed(raw)), 400)
        signal.assert_not_called()

    def test_legacy_receiver_does_not_drop_a_season_return(self):
        signal = Mock()
        receiver = core.Receiver(SECRET, lambda home: home == HOME, signal)
        cooling = (b'{"push_type":"home_event_changed","user_id":"demo-user",'
                   b'"home":{"id":"demo-home","temperature_control_mode":"cooling"}}')
        heating = (b'{"push_type":"home_event_changed","user_id":"demo-user",'
                   b'"extra_params":{"home":{"id":"demo-home",'
                   b'"temperature_control_mode":"heating"}}}')
        for raw in (cooling, cooling, heating, cooling):
            self.assertEqual(receiver.accept(raw, signed(raw)), 200)
        self.assertEqual(signal.call_count, 3)
        self.assertEqual(receiver.counts["duplicate"], 1)
