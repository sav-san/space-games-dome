import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
import threading
import queue
from unittest.mock import AsyncMock
from dome_agent.autonomy import candidates, Autonomy
from dome_agent.chat import Chat
from dome_agent.core import Log


def setup_agent(folder):
    def n(**v):return dict(online=True,status='OK',updated_at=100,**v)
    nodes={'smoke_detector_01':n(smoke_detected=False),'smoke_detector_02':n(smoke_detected=False),
    'air_quality_sensor_01':n(co_ppm=0,chem_ppm=0),'dome_sealing_01':n(leak_detected=False,sealed_zone=None),
    'cnc_01':n(state='RUNNING'),'fume_extraction_01':n(enabled=False,motor_state='OFF'),
    'climate_sensor_01':n(zone='FabLab',co2_ppm=1500),'climate_sensor_02':n(zone='FabLab',co2_ppm=1550),
    'supply_ventilation_01':n(state='RUNNING',mode='FRESH_AIR',speed_pct=50,filter_state='OK')}
    a=SimpleNamespace(role='team',frame={'timestamp':100,'nodes':nodes},last_received=time.monotonic(),server_changed=time.monotonic(),generation=1,issues=[],detector=SimpleNamespace(stale=20),state=Path(folder),log=Log(folder))
    a.search=SimpleNamespace(search=lambda *args:[{'id':'r1','text':'case','metadata':{'document_role':'incident_case'}}])
    result={'candidate_id':'extraction_on','confidence':.96,'reason':'Вытяжка нужна работающему ЧПУ','rag_refs':['r1']}
    async def request(p,purpose,validator):return validator(result)
    a.llm=SimpleNamespace(available=lambda:True,model='fake',request_json=AsyncMock(side_effect=request))
    c=SimpleNamespace(catalog=[{'node_id':'fume_extraction_01','controls':['turn_on']},{'node_id':'supply_ventilation_01','controls':['set_speed']}],refresh_catalog=AsyncMock())
    return a,c,result

class Predicates(unittest.TestCase):
    def test_live_conditions_and_blockers(self):
        with tempfile.TemporaryDirectory() as d:
            a,c,r=setup_agent(d)
            self.assertEqual({x['id'] for x in candidates(a)},{'extraction_on','ventilation_up'})
            a.frame['nodes']['climate_sensor_02']['co2_ppm']=400
            self.assertNotIn('ventilation_up',{x['id'] for x in candidates(a)})
            a.frame['nodes']['smoke_detector_01']['smoke_detected']=True
            self.assertEqual(candidates(a),[]);a.log.close()
    def test_bad_quality_and_stale_stream(self):
        with tempfile.TemporaryDirectory() as d:
            a,c,r=setup_agent(d)
            a.frame['nodes']['climate_sensor_01']['confidence']=.2
            self.assertNotIn('ventilation_up',{x['id'] for x in candidates(a)})
            a.last_received-=20;self.assertEqual(candidates(a),[]);a.log.close()

class Execution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.a,self.c,self.result=setup_agent(self.temp.name);self.events=[]
        self.ex=Chat(self.a,self.c,queue.Queue(),self.events.append)
        self.ex.execute=AsyncMock(return_value=True)
        self.auto=Autonomy(self.a,self.c,self.ex,self.events.append,threading.Event())
        self.auto.seen={'extraction_on':(time.monotonic()-16,99),'ventilation_up':(time.monotonic()-16,99)}
    async def asyncTearDown(self):self.a.log.close();self.temp.cleanup()
    async def test_executes_reports_and_does_not_repeat(self):
        await self.auto.tick();self.ex.execute.assert_awaited_once()
        self.assertEqual(self.auto.records['extraction_on']['status'],'confirmed')
        self.assertTrue(any('подтверждена' in e['text'] for e in self.events))
        await self.auto.tick();self.ex.execute.assert_awaited_once()
    async def test_low_confidence_is_not_authority(self):
        self.result['confidence']=.94;await self.auto.tick();self.ex.execute.assert_not_awaited()
    async def test_unknown_reference_rejected(self):
        self.result['rag_refs']=['fake']
        with self.assertRaises(ValueError):await self.auto.tick()
        self.ex.execute.assert_not_awaited()
    async def test_changed_telemetry_after_model_answer_blocks_action(self):
        async def changed(*args):
            self.a.frame['nodes']['fume_extraction_01']['enabled']=True
            return self.result
        self.a.llm.request_json=changed;await self.auto.tick();self.ex.execute.assert_not_awaited()
    async def test_uncertain_result_persists_halt_after_restart(self):
        self.ex.execute.return_value=False;await self.auto.tick();self.assertTrue(self.auto.halted)
        restarted=Autonomy(self.a,self.c,self.ex,self.events.append,threading.Event());self.assertTrue(restarted.halted)
    async def test_manual_plan_and_disable_block_autonomy(self):
        self.ex.pending=('plan',);await self.auto.tick();self.ex.execute.assert_not_awaited()
        self.ex.pending=None;self.auto.disable.set();await self.auto.tick();self.ex.execute.assert_not_awaited()
    async def test_malformed_confidence_never_coerced(self):
        self.result['confidence']=96
        with self.assertRaises(ValueError):await self.auto.tick()
        self.ex.execute.assert_not_awaited()
    async def test_no_rights_blocks_before_llm(self):
        self.c.catalog=[];await self.auto.tick();self.ex.execute.assert_not_awaited();self.a.llm.request_json.assert_not_awaited()
