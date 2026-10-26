import asyncio
import io
import json
import os
import queue
import time
import unittest
import urllib.error
from types import SimpleNamespace as NS
from unittest.mock import patch,AsyncMock
from dome_agent.llm import LLM,LLMRequestError
from dome_agent.chat import Chat,guard,validate_command,normalize_plan,verified
from test_chat import Log


def response(payload):return {'model':'test','choices':[{'message':{'content':json.dumps({'answer':'ok','commands':[]})},'finish_reason':'stop'}]}

def factory():
    def n(**kw):return {'online':True,'status':'OK','updated_at':100,**kw}
    nodes={'printer_3d_01':n(state='IDLE',nozzle_target_c=0,bed_target_c=0,heater_power_pct=0),
           'cnc_01':n(state='RUNNING',spindle_rpm=10000),
           'smart_panel_01':n(lines=[{'line':3,'name':'3D-принтер','state':'ON'},{'line':4,'name':'ЧПУ-фрезер','state':'ON'},{'line':6,'name':'Обычное освещение','state':'ON'},{'line':7,'name':'Аварийное освещение','state':'OFF'}]),
           'water_pump_01':n(state='ON',running=True),'equipment_cooling_01':n(state='ON')}
    catalog=[{'node_id':'smart_panel_01','controls':['line_on','line_off']},{'node_id':'cnc_01','controls':['pause','abort','start_job']},{'node_id':'printer_3d_01','controls':['start_job']},{'node_id':'water_pump_01','controls':['turn_on','turn_off']},{'node_id':'equipment_cooling_01','controls':['turn_off']}]
    a=NS(frame={'timestamp':100,'nodes':nodes},role='team',generation=1,last_received=time.monotonic(),server_changed=time.monotonic(),issues=[],log=Log(),detector=NS(stale=20))
    c=NS(catalog=catalog,role='team',refresh_catalog=AsyncMock(return_value=catalog),control=AsyncMock(return_value={'success':True}))
    return a,c

def cmd(node='smart_panel_01',action='line_off',value=6):return {'node_id':node,'action':action,'value':value,'delay_seconds':0}

class DeviceScope(unittest.TestCase):
    def test_light_can_toggle_with_cnc_running(self):
        a,c=factory()
        guard(a,c,cmd());guard(a,c,cmd(action='line_on',value=7))
    def test_idle_printer_can_power_off_with_cnc_running(self):
        a,c=factory();guard(a,c,cmd(value=3))
    def test_busy_cnc_power_protected(self):
        a,c=factory()
        with self.assertRaisesRegex(ValueError,'cnc_01 находится в RUNNING'):guard(a,c,cmd(value=4))
    def test_water_independent(self):
        a,c=factory();guard(a,c,cmd('water_pump_01','turn_off',None))
    def test_cooling_dependency(self):
        a,c=factory()
        with self.assertRaisesRegex(ValueError,'cnc_01'):guard(a,c,cmd('equipment_cooling_01','turn_off',None))
    def test_stop_allowed_during_hold_and_hazard_if_server_grants(self):
        a,c=factory();a.frame['nodes']['cnc_01'].update(hold_reason='IL_SMOKE',alarm=True)
        a.issues=[{'id':'smoke','code':'HAZARD','node_id':'smoke_detector_01','severity':'critical','category':'safety'}]
        guard(a,c,cmd('cnc_01','abort',None));guard(a,c,cmd('cnc_01','pause',None))
    def test_stop_absent_is_rejected(self):
        a,c=factory()
        with self.assertRaisesRegex(ValueError,'describe_nodes'):guard(a,c,cmd('printer_3d_01','abort',None))
    def test_line_mapping_follows_names_not_numbers(self):
        a,c=factory();lines=a.frame['nodes']['smart_panel_01']['lines'];lines[0]['name']='ЧПУ';lines[1]['name']='Освещение'
        guard(a,c,cmd(value=4))
        with self.assertRaises(ValueError):guard(a,c,cmd(value=3))
    def test_hot_idle_printer_is_not_unpowered(self):
        a,c=factory();a.frame['nodes']['printer_3d_01']['bed_target_c']=60
        with self.assertRaisesRegex(ValueError,'нагрев'):guard(a,c,cmd(value=3))
    def test_stop_verification(self):
        self.assertTrue(verified(cmd('cnc_01','pause',None),{}, {'state':'PAUSED'}))
        self.assertFalse(verified(cmd('cnc_01','abort',None),{}, {'success':True,'state':'RUNNING'}))
    def test_multistep_plan(self):
        a,c=factory();commands=[cmd(),cmd(value=3)]
        self.assertEqual(len(normalize_plan({'commands':commands},c.catalog)),2)
        with self.assertRaises(ValueError):normalize_plan({'commands':[cmd(),cmd('printer_3d_01','abort',None)]},c.catalog)

class LLMRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env=patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test','AGENT_LLM_MAX_CALLS':'1'});self.env.start()
    async def asyncTearDown(self):self.env.stop()
    async def test_no_legacy_budget_or_success_cooldown(self):
        llm=LLM(Log());llm._post=response
        for _ in range(95):self.assertEqual((await llm.request_json({},'chat'))['answer'],'ok')
        self.assertEqual(llm.calls,95);self.assertTrue(llm.available())
    async def test_timeout_not_quota_and_next_call_works(self):
        llm=LLM(Log());llm._post=lambda p:(_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(LLMRequestError) as ctx:await llm.request_json({},'chat')
        self.assertEqual(ctx.exception.kind,'timeout')
        llm._post=response;self.assertEqual((await llm.request_json({},'chat'))['answer'],'ok')
    async def test_real_http_codes_distinguished(self):
        for status,expected in [(401,'ключ'),(402,'баланс'),(429,'сервер ограничил'),(503,'Ошибка API')]:
            llm=LLM(Log())
            def failed(p):raise urllib.error.HTTPError('https://example.invalid',status,'error',{'Retry-After':'7'} if status==429 else {},io.BytesIO(b'{"error":{"message":"provider detail"}}'))
            llm._post=failed
            with self.assertRaises(LLMRequestError) as ctx:await llm.request_json({},'chat')
            self.assertEqual(ctx.exception.status,status);self.assertIn(expected,str(ctx.exception))
            if status==429:self.assertEqual(ctx.exception.retry_after,'7')
    async def test_invalid_json_and_truncation_not_limits(self):
        llm=LLM(Log());llm._post=lambda p:{'choices':[{'message':{'content':'not json'}}]}
        with self.assertRaises(LLMRequestError) as ctx:await llm.request_json({},'chat')
        self.assertEqual(ctx.exception.kind,'response_format')
        llm._post=lambda p:{'choices':[{'finish_reason':'length','message':{'content':'{'}}]}
        with self.assertRaises(LLMRequestError) as ctx:await llm.request_json({},'chat')
        self.assertEqual(ctx.exception.kind,'response_truncated')
    async def test_chat_does_not_wait_for_background_diagnosis(self):
        llm=LLM(Log());started=asyncio.Event();release=asyncio.Event()
        async def worker(fn,payload):
            if payload.get('slow'):started.set();await release.wait()
            return response(payload)
        with patch('dome_agent.llm.asyncio.to_thread',side_effect=worker):
            task=asyncio.create_task(llm.request_json({'slow':True},'diagnosis'));await started.wait()
            result=await asyncio.wait_for(llm.request_json({},'chat'),.5)
            self.assertEqual(result['answer'],'ok');self.assertFalse(task.done());release.set();await task

class PlanFlow(unittest.IsolatedAsyncioTestCase):
    async def test_second_step_not_executed_after_unconfirmed_first(self):
        a,c=factory();out=[];chat=Chat(a,c,queue.Queue(),out.append);chat.execute=AsyncMock(return_value=False)
        await chat.execute_plan({'commands':[cmd(),cmd(value=3)]})
        self.assertEqual(chat.execute.await_count,1)
    async def test_steps_run_individually(self):
        a,c=factory();chat=Chat(a,c,queue.Queue(),lambda x:None);chat.execute=AsyncMock(return_value=True)
        await chat.execute_plan({'commands':[cmd(),cmd(value=3)]})
        self.assertEqual(chat.execute.await_count,2);self.assertEqual(c.refresh_catalog.await_count,2)
    async def test_cancel_prevents_steps(self):
        a,c=factory();chat=Chat(a,c,queue.Queue(),lambda x:None);chat.execute=AsyncMock(return_value=True);chat.cancel_event.set()
        await chat.execute_plan({'commands':[cmd(),cmd(value=3)]})
        chat.execute.assert_not_awaited()
    async def test_switch_budget_is_per_line(self):
        a,c=factory();chat=Chat(a,c,queue.Queue(),lambda x:None)
        chat.line_times[('smart_panel_01',6)]=[time.monotonic()]*3
        with self.assertRaisesRegex(ValueError,'ЭТОЙ линии'):await chat.execute(cmd())
        # A distinct line is not rejected by the other line's switching count.
        c.control=AsyncMock(return_value={'success':False,'error':'mock rejection'})
        self.assertFalse(await chat.execute(cmd(action='line_on',value=7)))
        c.control.assert_awaited_once()

class ActualExecution(unittest.IsolatedAsyncioTestCase):
    async def test_abort_then_power_off_with_fresh_observation(self):
        a,c=factory();out=[]
        async def control(node,action,value):
            a.frame['timestamp']+=1
            if action=='abort':a.frame['nodes'][node].update(state='IDLE',spindle_rpm=0,spindle_target_rpm=0,updated_at=a.frame['timestamp'])
            elif action=='line_off':
                next(l for l in a.frame['nodes'][node]['lines'] if l['line']==value)['state']='OFF'
                a.frame['nodes'][node]['updated_at']=a.frame['timestamp']
            return {'success':True}
        c.control=AsyncMock(side_effect=control)
        chat=Chat(a,c,queue.Queue(),out.append)
        await chat.execute_plan({'commands':[cmd('cnc_01','abort',None),cmd(value=4)]})
        self.assertEqual(c.control.await_count,2)
        self.assertEqual(a.frame['nodes']['smart_panel_01']['lines'][1]['state'],'OFF')
        self.assertIn('План завершён',out[-1]['text'])
    async def test_chat_plans_two_lights_through_common_llm(self):
        a,c=factory();a.search=NS(search=lambda *args:[])
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test'}):
            a.llm=LLM(Log())
            answer={'answer':'Подготовлены два действия','commands':[cmd(),cmd(action='line_on',value=7)]}
            a.llm._post=lambda payload:{'choices':[{'message':{'content':json.dumps(answer)}}]}
            out=[];chat=Chat(a,c,queue.Queue(),out.append)
            await chat.ask('Выключи обычный свет и включи аварийный')
            self.assertEqual(len(out[-1]['plan']['commands']),2)
            c.control.assert_not_awaited()
            c.refresh_catalog.assert_awaited_once()
