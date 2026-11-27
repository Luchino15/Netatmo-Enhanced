"""Synthetic fixtures following the observed Netatmo webhook structures."""

HOME = "demo-home"
ROOM = "demo-room"
MODULE = "demo-module"
WINTER = "demo-winter"
WINTER_ALT = "demo-winter-alt"
SUMMER = "demo-summer"


def home_payload(*, nested=False, root_id=False, home_id=HOME, **fields):
    values = {"home": {"id": home_id, **fields.pop("home", {})}, **fields}
    route = {"home_id": home_id} if root_id else {}
    return {"push_type": "home_event_changed", "user_id": "demo-user", **route,
            **({"extra_params": values} if nested else values)}


def device_payload(*, rooms=None, modules=None, home_id=HOME):
    home = {}
    if rooms is not None:
        home["rooms"] = rooms
    if modules is not None:
        home["modules"] = modules
    return {"push_type": "device_event", "user_id": "demo-user", "home_id": home_id,
            "extra_params": {"home": home}}


def room_event(sequence=None, *, room_id=ROOM, **fields):
    order = {"sequence_id": sequence} if sequence is not None else {}
    return device_payload(rooms=[{"id": room_id, **order, **fields}])


def module_event(sequence, boiler, cooler, *, module_id=MODULE, module_type="BNS"):
    return device_payload(modules=[{
        "id": module_id, "sequence_id": sequence, "type": module_type,
        "boiler_status": boiler, "cooler_status": cooler,
    }])


def schedule_event(schedule, *, nested=False):
    return home_payload(nested=nested, event_type="schedule", schedule_id=schedule)


def season_event(season, *, nested=False):
    if season == "cooling":
        return home_payload(nested=nested, home={
            "schedule_id": SUMMER, "cooling_mode": "schedule",
            "temperature_control_mode": "cooling",
        })
    return home_payload(nested=nested, event_type="therm_mode", mode="schedule", home={
        "schedule_id": WINTER, "therm_mode": "schedule",
        "temperature_control_mode": "heating",
    })


def heating_mode_event(mode):
    return home_payload(nested=True, root_id=True, event_type="therm_mode", mode=mode,
                        home={"schedule_id": WINTER, "therm_mode": mode})
