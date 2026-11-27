"""Validated payload construction, no Home Assistant dependency."""
from math import isfinite


def command(state, kind, value):
    season = state.get("season")
    prefix = {"heating": "therm", "cooling": "cooling"}.get(season)
    if not prefix or state.get("reachable") is False:
        raise ValueError("Stagione sconosciuta o termostato non raggiungibile")
    hid, rid = state["home_id"], state["room_id"]
    if kind == "temperature":
        low = 7 if season == "heating" else 16
        if type(value) not in (int, float) or not isfinite(value) or not low <= value <= 30 or value * 2 != round(value * 2):
            raise ValueError("Temperatura fuori dall'intervallo di collaudo o passo non valido")
        return "setstate", None, {"home": {"id": hid, "rooms": [{"id": rid, f"{prefix}_setpoint_mode": "manual", f"{prefix}_setpoint_temperature": value}]}}
    if kind == "preset":
        if season != "heating" or value not in {"schedule", "away", "hg"}:
            raise ValueError("Modalità non ancora abilitata per questa stagione")
        if value == "schedule" and state.get("room_mode") == "manual" and state.get("home_mode") in {"schedule", "home"}:
            return "setstate", None, {"home": {"id": hid, "rooms": [{"id": rid, "therm_setpoint_mode": "home"}]}}
        return "setthermmode", {"home_id": hid, "mode": value}, None
    if kind == "schedule":
        sid = state["schedule_options"].get(value)
        if not sid:
            raise ValueError("Programmazione non disponibile nella stagione attiva")
        return "switchhomeschedule", {"home_id": hid, "schedule_id": sid}, None
    raise ValueError("Comando sconosciuto")


def confirmed(before, after, kind, value):
    if before.get("season") != after.get("season"):
        return False
    if kind == "temperature":
        return after.get("mode") == "manual" and after.get("target_temperature") == value
    if kind == "preset":
        if value == "schedule":
            return (after.get("mode") in {"home", "schedule"}
                    and after.get("room_mode") in {"home", "schedule"}
                    and after.get("home_mode") in {"home", "schedule"})
        return after.get("mode") == value
    if kind == "schedule":
        return after.get("active_schedule_id") == before.get("schedule_options", {}).get(value)
    return False
