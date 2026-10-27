import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from test_v08 import factory,cmd
from dome_agent import access
from dome_agent.chat import Chat,guard,validate_command,verified,constraints
from dome_agent.core import Log
from tools.check_logs import validate as validate_logs


def fixture():
    a,c=factory()
    for node,fields,actions in (
        ('thermal_insulation_01',{'locks':['LOCKED','LOCKED'],'doors':['CLOSED','CLOSED']},['set_lock']),
        ('thermal_insulation_02',{'lock':'LOCKED','doors':['CLOSED','CLOSED']},['set_lock']),
        ('access_control_01',{'doors':{'FABLAB':{'state':'CLOSED','locked':True}},'lockdown':False},['lock_door','unlock_door']),
    ):
        a.frame['nodes'][node]={'online':True,'status':'OK','updated_at':100,**fields}
        c.catalog.append({'node_id':node,'controls':actions})
    a.llm=NS(request_json=AsyncMock(side_effect=AssertionError('No model call expected')))
    return a,c


def unlock(index=0):return cmd('thermal_insulation_01','set_lock',{'lock':index,'locked':False})


class Access(unittest.TestCase):
    def test_lock_control_allowed_while_unrelated_cnc_runs(self):
        a,c=fixture();guard(a,c,unlock())
        guard(a,c,cmd('thermal_insulation_02','set_lock',False))
        guard(a,c,cmd('access_control_01','unlock_door','FABLAB'))
    def test_parameter_validation(self):
        a,c=fixture()
        for value in ({'lock':True,'locked':False},{'lock':2,'locked':False},{'lock':0,'locked':0},{'lock':0,'locked':False,'force':True},False):
            with self.assertRaises(ValueError):validate_command(cmd('thermal_insulation_01','set_lock',value),c.catalog)
        with self.assertRaises(ValueError):validate_command(cmd('access_control_01','unlock_door','UNKNOWN'),c.catalog)
    def test_operator_only_is_not_permission(self):
        a,c=fixture();c.catalog[-3].update(controls=[],operator_controls=['set_lock'])
        with self.assertRaisesRegex(ValueError,'operator_controls'):guard(a,c,unlock())
    def test_active_paused_stale_or_hot_printer_blocks_unlock(self):
        for changes in ({'state':'PRINTING','webhook_state':'COMPLETED'},{'state':'PAUSED'},{'updated_at':1},{'nozzle_target_c':200}):
            a,c=fixture();a.frame['nodes']['printer_3d_01'].update(changes)
            with self.assertRaises(ValueError):guard(a,c,unlock())
    def test_lock_closed_box_while_printing(self):
        a,c=fixture();a.frame['nodes']['printer_3d_01']['state']='PRINTING'
        guard(a,c,cmd('thermal_insulation_01','set_lock',{'lock':0,'locked':True}))
        a.frame['nodes']['thermal_insulation_01']['doors'][0]='OPEN'
        with self.assertRaisesRegex(ValueError,'физически закрыть'):guard(a,c,cmd('thermal_insulation_01','set_lock',{'lock':0,'locked':True}))
    def test_guest_stale_target_and_lockdown(self):
        a,c=fixture();a.role='guest'
        with self.assertRaises(ValueError):guard(a,c,unlock())
        a.role='team';a.frame['nodes']['thermal_insulation_01']['updated_at']=0
        with self.assertRaises(ValueError):guard(a,c,unlock())
        a.frame['nodes']['access_control_01']['lockdown']=True
        with self.assertRaisesRegex(ValueError,'lockdown'):guard(a,c,cmd('access_control_01','unlock_door','FABLAB'))
    def test_verification_needs_correct_lock_not_open_door_or_ack(self):
        self.assertFalse(verified(unlock(),{}, {'success':True,'doors':['OPEN','CLOSED']}))
        self.assertFalse(verified(unlock(),{}, {'locks':['LOCKED','UNLOCKED']}))
        self.assertTrue(verified(unlock(),{}, {'locks':['UNLOCKED','LOCKED']}))
        self.assertTrue(verified(cmd('thermal_insulation_02','set_lock',False),{}, {'lock':'UNLOCKED'}))
        self.assertTrue(verified(cmd('access_control_01','unlock_door','FABLAB'),{}, {'doors':{'FABLAB':{'locked':False}}}))
        self.assertFalse(verified(cmd('access_control_01','unlock_door','FABLAB'),{}, {'doors':{'FABLAB':{'locked':0}}}))
    def test_direct_intents_and_ambiguity(self):
        self.assertEqual(access.direct_request('Открой дверцу 1 FabLab')['commands'],[unlock()])
        self.assertEqual(len(access.direct_request('Разблокируй обе дверцы бокса Фаблаб')['commands']),2)
        self.assertEqual(access.direct_request('открой дверцу fablab')['commands'],[])
        for text in ('Не открой дверцу 1 FabLab','Можно ли открыть дверцу 1 FabLab?','Открой дверцу 1 FabLab через час','открой дверь помещения FABLAB','открой дверцу 1 FabLab 2'):
            self.assertIsNone(access.direct_request(text))
    def test_constraints_include_actual_reason(self):
        a,c=fixture();a.frame['nodes']['printer_3d_01']['state']='PRINTING'
        rows=[r for r in constraints(a,c) if r['node_id']=='thermal_insulation_01' and r['value']==unlock()['value']]
        self.assertEqual(len(rows),1);self.assertIn('PRINTING',rows[0]['blocked_reason'])


class ChatAccess(unittest.IsolatedAsyncioTestCase):
    async def test_direct_request_prepares_only_and_no_model(self):
        a,c=fixture();out=[];chat=Chat(a,c,queue.Queue(),out.append)
        await chat.ask('Открой дверцу 1 FabLab')
        self.assertEqual(chat.pending[0]['commands'],[unlock()]);c.control.assert_not_awaited();a.llm.request_json.assert_not_awaited()
        self.assertIn('физически',out[-1]['text'])
    async def test_direct_request_checks_current_rights_and_state(self):
        a,c=fixture();out=[];chat=Chat(a,c,queue.Queue(),out.append)
        a.frame['nodes']['printer_3d_01']['state']='PRINTING'
        await chat.ask('Открой дверцу 1 FabLab');self.assertIsNone(chat.pending);self.assertIn('PRINTING',out[-1]['text'])
        a.frame['nodes']['printer_3d_01']['state']='IDLE';c.catalog[-3]['controls']=[]
        await chat.ask('Открой дверцу 1 FabLab');self.assertIsNone(chat.pending);self.assertIn('describe_nodes',out[-1]['text'])
    async def test_confirmed_execution_and_log_chain(self):
        a,c=fixture()
        with tempfile.TemporaryDirectory() as d:
            a.log=Log(d);out=[];chat=Chat(a,c,queue.Queue(),out.append)
            async def control(node,action,value):
                a.frame['timestamp']=101;a.frame['nodes'][node].update(updated_at=101,locks=['UNLOCKED','LOCKED'])
                return {'success':True}
            c.control=AsyncMock(side_effect=control)
            await chat.ask('Открой дверцу 1 FabLab')
            await chat.execute_plan(chat.pending[0]);c.control.assert_awaited_once_with('thermal_insulation_01','set_lock',{'lock':0,'locked':False})
            a.log.close();rows=[json.loads(s) for s in next(Path(d).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(validate_logs(rows),[])
            row=next(r for r in rows if r['kind']=='verification');self.assertTrue(row['ok']);self.assertEqual(row['expected']['state'],'UNLOCKED');self.assertEqual(row['observed']['locks'][0],'UNLOCKED')
    async def test_state_changed_after_plan_blocks_execution(self):
        a,c=fixture();chat=Chat(a,c,queue.Queue(),lambda _:None)
        await chat.ask('Открой дверцу 1 FabLab');a.frame['nodes']['printer_3d_01']['state']='PRINTING'
        with self.assertRaises(ValueError):await chat.execute_plan(chat.pending[0])
        c.control.assert_not_awaited()
