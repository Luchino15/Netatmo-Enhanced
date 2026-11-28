"""Validated payload construction, no Home Assistant dependency."""
BOOST_DURATIONS = (30, 60, 90)
MANUAL_MAX_MINUTES = 720
DEADLINE_TOLERANCE_SECONDS = 60
SEASON_PREFIX = {"heating": "therm", "cooling": "cooling"}


def requested_duration(kind, value):
    if kind == "boost":
        if type(value) is not int or value not in BOOST_DURATIONS:
            raise ValueError("Durata Boost consentita: 30, 60 o 90 minuti")
        return value
    if kind == "temperature_duration":
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or type(value[1]) is not int or not 1 <= value[1] <= MANUAL_MAX_MINUTES):
            raise ValueError("Durata manuale consentita: da 1 a 720 minuti")
        return value[1]
    return None


def room_command(hid, rid, **fields):
    return "setstate", None, {"home": {"id": hid, "rooms": [{"id": rid, **fields}]}}


def command(state, kind, value, *, deadline=None):
    season = state.get("season")
    prefix = SEASON_PREFIX.get(season)
    if not prefix or state.get("reachable") is False:
        raise ValueError("Stagione sconosciuta o termostato non raggiungibile")
    hid, rid = state["home_id"], state["room_id"]
    duration = requested_duration(kind, value)
    if duration is not None and (type(deadline) is not int or deadline <= 0):
        raise ValueError("Scadenza del comando mancante o non valida")
    if kind in {"temperature", "temperature_duration"}:
        temperature = value[0] if duration is not None else value
        low = 7 if season == "heating" else 16
        if (type(temperature) not in (int, float) or not low <= temperature <= 30
                or temperature * 2 != round(temperature * 2)):
            raise ValueError("Temperatura fuori dall'intervallo di collaudo o passo non valido")
        fields = {f"{prefix}_setpoint_mode": "manual", f"{prefix}_setpoint_temperature": temperature}
        if duration is not None:
            fields[f"{prefix}_setpoint_end_time"] = deadline
        return room_command(hid, rid, **fields)
    if kind in {"boost", "stop_boost"}:
        if kind == "boost":
            return room_command(hid, rid, **{
                f"{prefix}_setpoint_mode": "max", f"{prefix}_setpoint_end_time": deadline,
            })
        if state.get("room_mode") != "max":
            raise ValueError("Boost non attivo")
        return room_command(hid, rid, **{f"{prefix}_setpoint_mode": "home"})
    if kind == "power_off":
        if season != "cooling":
            raise ValueError("Lo spegnimento della stanza è disponibile in raffrescamento")
        return room_command(hid, rid, cooling_setpoint_mode="off")
    if kind == "preset":
        if season == "cooling" and value == "schedule" and state.get("home_mode") in {"home", "schedule"}:
            return room_command(hid, rid, cooling_setpoint_mode="home")
        if season != "heating" or value not in {"schedule", "away", "hg"}:
            raise ValueError("Modalità non ancora abilitata per questa stagione")
        if value == "schedule" and state.get("room_mode") in {"manual", "max"} and state.get("home_mode") in {"schedule", "home"}:
            return room_command(hid, rid, therm_setpoint_mode="home")
        return "setthermmode", {"home_id": hid, "mode": value}, None
    if kind == "schedule":
        sid = state["schedule_options"].get(value)
        if not sid:
            raise ValueError("Programmazione non disponibile nella stagione attiva")
        return "switchhomeschedule", {"home_id": hid, "schedule_id": sid}, None
    raise ValueError("Comando sconosciuto")


def confirmed(before, after, kind, value, *, deadline=None):
    if before.get("season") != after.get("season"):
        return False
    if kind == "temperature":
        return after.get("mode") == "manual" and after.get("target_temperature") == value
    if kind in {"boost", "temperature_duration"}:
        expiry = after.get("setpoint_end_time")
        if (deadline is None or type(expiry) not in (int, float)
                or not deadline - DEADLINE_TOLERANCE_SECONDS <= expiry <= deadline + DEADLINE_TOLERANCE_SECONDS
                or (expiry == before.get("setpoint_end_time") and expiry != deadline)):
            return False
        if kind == "boost":
            return after.get("room_mode") == "max" and after.get("target_temperature") is None
        return after.get("room_mode") == "manual" and after.get("target_temperature") == value[0]
    if kind == "stop_boost":
        return after.get("room_mode") == "home" and after.get("setpoint_end_time") is None
    if kind == "power_off":
        return after.get("room_mode") == "off"
    if kind == "preset":
        if value == "schedule":
            return (after.get("mode") in {"home", "schedule"}
                    and after.get("room_mode") in {"home", "schedule"}
                    and after.get("home_mode") in {"home", "schedule"})
        return after.get("mode") == value
    if kind == "schedule":
        return after.get("active_schedule_id") == before.get("schedule_options", {}).get(value)
    return False
