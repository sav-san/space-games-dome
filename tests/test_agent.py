import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dome_agent.core import Detector,Log,Search
from dome_agent.llm import LLM
from dome_agent.client import DomeClient
import websockets

class Rules(unittest.TestCase):
    def test_conflicting_co2_not_automatically_false(self):
        nodes={f'climate_sensor_0{i}':{'co2_ppm':v,'reading_delay_s':delay,'updated_at':100} for i,v,delay in [(1,3000,0),(2,770,900),(3,790,1800)]}
        out=Detector().analyze(nodes,100)
        co2=next(x for x in out if x['code']=='CO2')
        self.assertEqual(co2['category'],'conflicting_data');self.assertEqual(co2['evidence']['climate_sensor_02']['reading_delay_s'],900)
    def test_null_and_missing(self):
        d=Detector();a=d.analyze({'climate_sensor_01':{'co2_ppm':None}},100)
        self.assertTrue(a);self.assertEqual(d.analyze({},102)[0]['code'],'MISSING')
    def test_stale_not_used_for_co2(self):
        out=Detector().analyze({'climate_sensor_01':{'co2_ppm':3000,'updated_at':1}},100)
        self.assertEqual([x['code'] for x in out],['STALE'])
    def test_frozen_and_recovery(self):
        d=Detector();n={'x':{'updated_at':100}}
        self.assertFalse(d.analyze(n,100));self.assertEqual(d.analyze(n,130)[0]['code'],'STALE')
        self.assertFalse(d.analyze({'x':{'updated_at':131}},131))
    def test_webhook(self):
        out=Detector().analyze({'printer_3d_01':{'state':'PRINTING','webhook_state':'COMPLETED','progress_pct':33}},100)
        self.assertEqual(out[0]['code'],'WEBHOOK')
    def test_complete_not_fault(self):
        self.assertFalse(Detector().analyze({'printer_3d_01':{'state':'COMPLETED','webhook_state':'COMPLETED','progress_pct':100}},100))
    def test_clock(self):
        self.assertEqual(Detector().analyze({'edge_compute_01':{'clock_offset_s':7200}},100)[0]['code'],'CLOCK')
    def test_retrieval(self):
        s=Search(Path(__file__).resolve().parents[1]/'knowledge/chunks.jsonl')
        self.assertTrue(any('webhook' in r['text'].lower() for r in s.search('printer_3d_01 webhook COMPLETED PRINTING',5)))
    def test_model_fabricated_reference_rejected(self):
        h={'summary':'s','hypotheses':[{'category':'energy','cause':'test','confidence':.5,'evidence_issue_ids':['invented'],'rag_refs':[],'checks':[],'proposed_steps':[]}]}
        with self.assertRaises(ValueError):LLM.validate(h,{'real'},set())
    def test_redaction(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ['DOME_KEY']='secret-test-123';log=Log(d)
            log.write('error',error='contains secret-test-123');log.close()
            self.assertNotIn('secret-test-123',next(Path(d).glob('*.jsonl')).read_text())
            del os.environ['DOME_KEY']

class Network(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_telemetry_catalog_revocation(self):
        received=[];requests=[];states=[]
        async def server(ws):
            auth=json.loads(await ws.recv());self.assertEqual(auth['type'],'auth')
            await ws.send(json.dumps({'type':'auth_response','success':True,'role':'team','team':1}))
            req=json.loads(await ws.recv());requests.append(req)
            await ws.send(json.dumps({'type':'telemetry','timestamp':100,'data':{'nodes':{}}}))
            await ws.send(json.dumps({'type':'describe_nodes_response','request_id':req['request_id'],'success':True,'nodes':[{'node_id':'x','controls':[]}]}))
            await asyncio.sleep(.05)
            await ws.send(json.dumps({'type':'session_revoked','reason':'test'}))
        with tempfile.TemporaryDirectory() as d:
            log=Log(d)
            async with websockets.serve(server,'127.0.0.1',0) as srv:
                port=srv.sockets[0].getsockname()[1]
                c=DomeClient(f'ws://127.0.0.1:{port}','test',log,lambda m,g:received.append(m),lambda r,g:states.append(r))
                await asyncio.wait_for(c.session(),3)
                self.assertEqual(len(received),1);self.assertEqual(c.catalog[0]['node_id'],'x')
                with self.assertRaises(ValueError):await c.request('control')
            log.close()
            self.assertEqual([r['type'] for r in requests],['describe_nodes'])
    async def test_llm_failure_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            log=Log(d);llm=LLM(log);os.environ['DEEPSEEK_API_KEY']='test'
            def failed(_):raise TimeoutError()
            llm._post=failed
            self.assertIsNone(await llm.diagnose([],[],[]));self.assertTrue(llm.available());self.assertIn('таймаут',llm.last_diagnosis_error)
            log.close();del os.environ['DEEPSEEK_API_KEY']

if __name__=='__main__':unittest.main()

class MoreNetwork(unittest.IsolatedAsyncioTestCase):
    async def test_guest_does_not_query_devices(self):
        seen=[]
        async def server(ws):
            seen.append(json.loads(await ws.recv())['type'])
            await ws.send(json.dumps({'type':'auth_response','success':True,'role':'guest','team':1}))
            await ws.send(json.dumps({'type':'telemetry','role':'guest','timestamp':100,'data':{'nodes':{'meeting_01':{}}}}))
            await ws.close()
        with tempfile.TemporaryDirectory() as d:
            log=Log(d);frames=[]
            async with websockets.serve(server,'127.0.0.1',0) as srv:
                c=DomeClient(f'ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}','test',log,lambda m,g:frames.append(m),lambda r,g:None)
                await asyncio.wait_for(c.session(),2)
            log.close();self.assertEqual(seen,['auth']);self.assertEqual(frames,[])
    async def test_deepseek_valid_response(self):
        with tempfile.TemporaryDirectory() as d:
            log=Log(d);llm=LLM(log);os.environ['DEEPSEEK_API_KEY']='test'
            content={'summary':'Проверить данные','hypotheses':[{'category':'unknown','cause':'Недостаточно данных','confidence':.2,'evidence_issue_ids':['x'],'rag_refs':['r'],'checks':['Проверить updated_at'],'proposed_steps':[]}]}
            llm._post=lambda payload:{'model':payload['model'],'choices':[{'message':{'content':json.dumps(content)}}],'usage':{'prompt_tokens':10,'completion_tokens':20}}
            result=await llm.diagnose([{'id':'x'}],[{'id':'r','text':'text','metadata':{}}],[])
            self.assertEqual(result['summary'],'Проверить данные')
            log.close();del os.environ['DEEPSEEK_API_KEY']
