import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location("model", Path(__file__).resolve().parents[1] / "custom_components/netatmo_enhanced/model.py")
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)

class TestModel(unittest.TestCase):
    def setUp(self):
        self.room = {"id":"room","name":"Corridor"}
        self.module = {"id":"module","room_id":"room","type":"BNS"}
        self.home = {"id":"home","rooms":[self.room],"modules":[self.module],
            "temperature_control_mode":"heating","therm_mode":"schedule",
            "cooling_mode":"schedule","schedules":[
                {"type":"therm","name":"Winter","selected":True},
                {"type":"therm","name":"Test"},
                {"type":"cooling","name":"Summer","selected":True}]}
        self.status = {"rooms":[{"id":"room","therm_measured_temperature":23.6,
            "therm_setpoint_temperature":16,"therm_setpoint_mode":"home",
            "cooling_setpoint_temperature":26,"cooling_setpoint_mode":"home",
            "humidity":69}], "modules":[{"id":"module","reachable":True}]}
    def snap(self):
        return model.snapshot(self.home,self.status,self.room,self.module)
    def test_discovery(self):
        self.assertEqual(list(model.discover([self.home])),[("home","module")])
    def test_heating(self):
        self.assertEqual(self.snap()["target_temperature"],16)
        self.assertEqual(self.snap()["active_schedule"],"Winter")
    def test_cooling(self):
        self.status["temperature_control_mode"]="cooling"
        self.assertEqual(self.snap()["target_temperature"],26)
        self.assertEqual(self.snap()["active_schedule"],"Summer")
    def test_both_selected(self):
        self.assertEqual(self.snap()["heating_schedule"],"Winter")
        self.assertEqual(self.snap()["cooling_schedule"],"Summer")
    def test_away(self):
        self.status["therm_mode"]="away"
        self.assertEqual(self.snap()["mode"],"away")
    def test_manual(self):
        self.status["therm_mode"]="away"
        self.status["rooms"][0]["therm_setpoint_mode"]="manual"
        self.assertEqual(self.snap()["mode"],"manual")
        self.assertEqual(self.snap()["room_mode"],"manual")
        self.assertEqual(self.snap()["home_mode"],"away")
    def test_missing_cooling_target(self):
        self.status["temperature_control_mode"]="cooling"
        self.status["rooms"][0].pop("cooling_setpoint_temperature")
        self.assertIsNone(self.snap()["target_temperature"])
    def test_invalid_humidity(self):
        self.status["rooms"][0]["humidity"]=101
        self.assertIsNone(self.snap()["humidity"])
    def test_reachable(self):
        self.status["modules"][0]["reachable"]=False
        self.assertIs(self.snap()["reachable"],False)
    def test_unknown_season(self):
        self.status["temperature_control_mode"]="unknown"
        self.assertIsNone(self.snap()["active_schedule"])
        self.assertIsNone(self.snap()["target_temperature"])

if __name__ == "__main__":
    unittest.main()
