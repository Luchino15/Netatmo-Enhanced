import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location("commands", Path(__file__).resolve().parents[1]/"custom_components/netatmo_enhanced/commands.py")
c=importlib.util.module_from_spec(spec); spec.loader.exec_module(c)

class TestCommands(unittest.TestCase):
    def setUp(self):
        self.s={"season":"heating","home_id":"h","room_id":"r","reachable":True,"schedule_options":{"Winter":"a","Test":"b"}}
    def test_heat(self):
        self.assertEqual(c.command(self.s,"temperature",17), ("setstate",None,{"home":{"id":"h","rooms":[{"id":"r","therm_setpoint_mode":"manual","therm_setpoint_temperature":17}]}}))
    def test_cool(self):
        self.s["season"]="cooling"
        self.assertEqual(c.command(self.s,"temperature",26)[2]["home"]["rooms"][0]["cooling_setpoint_temperature"],26)
    def test_preset(self):
        self.assertEqual(c.command(self.s,"preset","away"),("setthermmode",{"home_id":"h","mode":"away"},None))
    def test_cooling_preset_rejected(self):
        self.s["season"]="cooling"
        with self.assertRaises(ValueError): c.command(self.s,"preset","hg")
    def test_bad_temp(self):
        for t in [31,6,17.1,float("nan"),True]:
            with self.assertRaises(ValueError): c.command(self.s,"temperature",t)
    def test_schedule(self):
        self.assertEqual(c.command(self.s,"schedule","Test"),("switchhomeschedule",{"home_id":"h","schedule_id":"b"},None))
    def test_wrong_schedule(self):
        with self.assertRaises(ValueError): c.command(self.s,"schedule","Summer")
    def test_unreachable(self):
        self.s["reachable"]=False
        with self.assertRaises(ValueError): c.command(self.s,"temperature",17)
    def test_confirmation(self):
        self.assertTrue(c.confirmed(self.s,{**self.s,"mode":"manual","target_temperature":17},"temperature",17))
        self.assertFalse(c.confirmed(self.s,{**self.s,"season":"cooling","mode":"manual","target_temperature":17},"temperature",17))
    def test_schedule_confirmation(self):
        self.assertTrue(c.confirmed(self.s,{**self.s,"active_schedule_id":"b"},"schedule","Test"))
    def test_schedule_return(self):
        self.assertTrue(c.confirmed(self.s,{**self.s,"mode":"home","room_mode":"home","home_mode":"schedule"},"preset","schedule"))

    def test_cancel_manual(self):
        state = {**self.s, "room_mode":"manual", "home_mode":"schedule"}
        self.assertEqual(c.command(state,"preset","schedule"), ("setstate",None,{"home":{"id":"h","rooms":[{"id":"r","therm_setpoint_mode":"home"}]}}))
    def test_global_return(self):
        for mode in ("away", "hg"):
            state = {**self.s, "room_mode":"home", "home_mode":mode}
            self.assertEqual(c.command(state,"preset","schedule"),("setthermmode",{"home_id":"h","mode":"schedule"},None))
    def test_global_and_manual(self):
        state = {**self.s,"room_mode":"manual","home_mode":"away"}
        self.assertEqual(c.command(state,"preset","schedule")[0],"setthermmode")
    def test_not_confirmed_if_room_manual(self):
        self.assertFalse(c.confirmed(self.s,{**self.s,"mode":"manual","room_mode":"manual","home_mode":"schedule"},"preset","schedule"))
    def test_not_confirmed_if_room_missing(self):
        self.assertFalse(c.confirmed(self.s,{**self.s,"mode":"schedule","home_mode":"schedule"},"preset","schedule"))
