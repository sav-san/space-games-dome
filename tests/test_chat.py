import asyncio
import json
import queue
import time
import unittest
from types import SimpleNamespace as NS
from dome_agent.chat import validate_command, guard, verified, Chat
from dome_agent.client import DomeClient

class Log:
    def write(self,*args,**kwargs):return 1
    def clean(self,x):return x

def fixture():
    catalog=[{'node_id':'printer_3d_01','controls':['home','start_job','resume','move']}]
    n={'online':True,'status':'OK','state':'IDLE','updated_at':100,'homed':True,'bed_clear':True,'files':['a.gcode']}
    a=NS(role='team',frame={'timestamp':100,'nodes':{'printer_3d_01':n}},last_received=time.monotonic(),server_changed=time.monotonic(),issues=[],generation=1,log=Log())
    return a,NS(catalog=catalog),{'node_id':'printer_3d_01','action':'start_job','value':'a.gcode','delay_seconds':0}

class Commands(unittest.TestCase):
    def test_valid_start(self):
        a,c,cmd=fixture();guard(a,c,cmd)
    def test_missing_file(self):
        a,c,cmd=fixture();cmd['value']='imaginary.gcode'
        with self.assertRaises(ValueError):guard(a,c,cmd)
    def test_occupied_bed(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01']['bed_clear']=False
        with self.assertRaises(ValueError):guard(a,c,cmd)
    def test_guest_stale_hold(self):
        for field,value in [('online',False),('updated_at',20),('hold_reason','IL_CO2')]:
            a,c,cmd=fixture();a.frame['nodes']['printer_3d_01'][field]=value
            with self.assertRaises(ValueError):guard(a,c,cmd)
        a,c,cmd=fixture();a.role='guest'
        with self.assertRaises(ValueError):guard(a,c,cmd)
    def test_no_admin_commands(self):
        a,c,cmd=fixture();cmd['action']='reset_interlock'
        with self.assertRaises(ValueError):validate_command(cmd,c.catalog)
    def test_nan_delay(self):
        a,c,cmd=fixture();cmd['delay_seconds']=float('nan')
        with self.assertRaises(ValueError):validate_command(cmd,c.catalog)
    def test_resume_not_repeated(self):
        a,c,cmd=fixture();cmd.update(action='resume',value=None);a.frame['nodes']['printer_3d_01'].update(state='PAUSED',resume_attempts=1)
        with self.assertRaises(ValueError):guard(a,c,cmd)
    def test_verification_not_ack(self):
        a,c,cmd=fixture()
        self.assertFalse(verified(cmd,{}, {'success':True}))
        self.assertTrue(verified(cmd,{}, {'state':'PRINTING','file_name':'a.gcode'}))
        self.assertFalse(verified(cmd,{}, {'state':'PRINTING','file_name':'other'}))

class Network(unittest.IsolatedAsyncioTestCase):
    async def test_control_envelope_and_guest(self):
        a,c,cmd=fixture();client=DomeClient('ws://example','key',Log(),lambda *x:None,lambda *x:None);client.catalog=c.catalog
        with self.assertRaises(ValueError):await client.control(cmd['node_id'],cmd['action'],cmd['value'])
        sent=[]
        async def send(raw):
            row=json.loads(raw);sent.append(row);client.pending[row['request_id']].set_result({'success':True})
        client.role='team';client.ws=NS(send=send)
        await client.control(cmd['node_id'],cmd['action'],cmd['value'])
        self.assertEqual(sent[0]['type'],'control');self.assertEqual(sent[0]['value'],'a.gcode')
    async def test_cancel_pending(self):
        a,c,cmd=fixture();inbox=queue.Queue();out=[];chat=Chat(a,c,inbox,out.append);chat.pending=({'id':'x',**cmd},time.monotonic(),1)
        inbox.put({'kind':'cancel'});task=asyncio.create_task(chat.run());await asyncio.sleep(.15);task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertIsNone(chat.pending);self.assertIsNone(chat.scheduled)
    async def test_schedule_cancels_on_reconnect(self):
        a,c,cmd=fixture();out=[];chat=Chat(a,c,queue.Queue(),out.append);chat.scheduled=(cmd,time.monotonic()+60,0)
        task=asyncio.create_task(chat.run());await asyncio.sleep(.15);task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertIsNone(chat.scheduled);self.assertIn('отменён',out[0]['text'])
