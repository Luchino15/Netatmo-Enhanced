"""Exercise the real command method with offline fake API responses."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from test_commands import c

source=Path(__file__).resolve().parents[1]/'custom_components/netatmo_enhanced/coordinator.py'
cls=next(x for x in ast.parse(source.read_text()).body if isinstance(x,ast.ClassDef) and x.name=='EnhancedCoordinator')
method=next(x for x in cls.body if isinstance(x,ast.AsyncFunctionDef) and x.name=='async_command')
class HomeAssistantError(Exception): pass
class UpdateFailed(Exception): pass
class ConfigEntryAuthFailed(Exception): pass
async def sleep(_): pass
ns=dict(asyncio=SimpleNamespace(sleep=sleep), command=c.command, confirmed=c.confirmed, NEW_COMMANDS=set(), SCOPES={'write'}, token_scopes=lambda token:set(token), HomeAssistantError=HomeAssistantError,UpdateFailed=UpdateFailed,ConfigEntryAuthFailed=ConfigEntryAuthFailed)
exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),ns)
run=ns['async_command']

def state(room,home,season='heating'):
    return dict(home_id='h',room_id='r',season=season,reachable=True,room_mode=room,home_mode=home,mode=home if room=='home' and home in ('away','hg') else room)

class TestReturn(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,initial,updates,expected,success=True):
        calls=[]; updates=iter(updates)
        obj=SimpleNamespace(command_lock=asyncio.Lock(),data={'m':initial},last_update_success=True,oauth=SimpleNamespace(token=['write']))
        async def refresh(source):
            self.assertEqual(source, "command")
            obj.data={'m':next(updates)}
        async def request(endpoint,**kwargs):
            calls.append(endpoint)
            return {'status':'ok'}
        obj.async_refresh_for=refresh;obj._request=request
        if success: await run(obj,'m','preset','schedule')
        else:
            with self.assertRaises(HomeAssistantError): await run(obj,'m','preset','schedule')
        self.assertEqual(calls,expected)
    async def test_manual_only(self):
        a=state('manual','schedule'); b=state('home','schedule')
        await self.exercise(a,[a,b],['setstate'])
    async def test_global_only(self):
        a=state('home','hg'); b=state('home','schedule')
        await self.exercise(a,[a,b],['setthermmode'])
    async def test_global_and_manual(self):
        a=state('manual','away'); b=state('manual','schedule'); d=state('home','schedule')
        await self.exercise(a,[a,b,d],['setthermmode','setstate'])
    async def test_no_repeat_when_unconfirmed(self):
        a=state('manual','schedule')
        await self.exercise(a,[a,a],['setstate'],False)
    async def test_no_second_command_when_season_changes(self):
        a=state('manual','away');b=state('manual','schedule','cooling')
        await self.exercise(a,[a,b],['setthermmode'],False)
