"""Offline regression of the actual coordinator method, without HA installed."""
import ast
import asyncio
from copy import deepcopy
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
import unittest
from test_model import model

source = Path(__file__).resolve().parents[1] / 'custom_components/netatmo_enhanced/coordinator.py'
parsed = ast.parse(source.read_text())
cls = next(x for x in parsed.body if isinstance(x, ast.ClassDef) and x.name == 'EnhancedCoordinator')
method = next(x for x in cls.body if isinstance(x, ast.AsyncFunctionDef) and x.name == '_async_update_data')
class UpdateFailed(Exception):
    pass
namespace = {'discover': model.discover, 'snapshot': model.snapshot, 'UpdateFailed': UpdateFailed,
             'monotonic': monotonic,
             '_LOGGER': SimpleNamespace(debug=lambda *a: None)}
exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
refresh = namespace['_async_update_data']

class TestRefresh(unittest.IsolatedAsyncioTestCase):
    async def test_changes_across_successive_reads(self):
        home = {'id':'h','rooms':[{'id':'r','name':'Corridor'}],
                'modules':[{'id':'m','room_id':'r','type':'BNS'}],
                'temperature_control_mode':'heating','therm_mode':'schedule','cooling_mode':'schedule',
                'schedules':[{'id':'a','name':'Winter','type':'therm','selected':True},
                             {'id':'b','name':'Test','type':'therm'},
                             {'id':'c','name':'Summer','type':'cooling','selected':True}]}
        status = {'id':'h','rooms':[{'id':'r','therm_measured_temperature':23.3,
                 'therm_setpoint_mode':'home','therm_setpoint_temperature':16,'humidity':69}],
                 'modules':[{'id':'m','reachable':True}]}
        calls=[]
        async def read(endpoint, params=None):
            calls.append(endpoint)
            return deepcopy({'homes':[home]} if endpoint=='homesdata' else {'home':status})
        c=SimpleNamespace(_read=read,guard={})
        async def check(**expected):
            result=(await refresh(c))['m']
            for key,value in expected.items():
                self.assertEqual(result[key],value,key)
        await check(season='heating',mode='home',active_schedule='Winter',target_temperature=16)
        home['therm_mode']='away'
        await check(mode='away')
        home['therm_mode']='schedule'
        home['schedules'][0]['selected']=False
        home['schedules'][1]['selected']=True
        await check(mode='home',active_schedule='Test')
        home['temperature_control_mode']='cooling'
        r=status['rooms'][0]
        r.pop('therm_setpoint_mode'); r.pop('therm_setpoint_temperature')
        r.update(cooling_setpoint_mode='home',cooling_setpoint_temperature=26)
        await check(season='cooling',target_temperature=26,active_schedule='Summer',heating_schedule='Test')
        home['temperature_control_mode']='heating'
        r.update(therm_setpoint_mode='home',therm_setpoint_temperature=16)
        await check(season='heating',target_temperature=16,active_schedule='Test')
        self.assertEqual(calls,['homesdata','homestatus']*5)

    async def test_invalid_topology_stops_before_status(self):
        calls=[]
        async def read(endpoint, params=None):
            calls.append(endpoint)
            return {}
        with self.assertRaises(UpdateFailed):
            await refresh(SimpleNamespace(_read=read,guard={}))
        self.assertEqual(calls,['homesdata'])

if __name__=='__main__':
    unittest.main()
