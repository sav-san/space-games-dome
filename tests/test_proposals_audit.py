import json
import tempfile
import time
import queue
import unittest
from unittest.mock import AsyncMock
from pathlib import Path
from test_v08 import factory,cmd
from dome_agent.core import Log
from dome_agent.chat import Chat
from dome_agent.proposals import Proposals
from tools.check_logs import validate

class Suggestions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.a,self.c=factory();self.a.issues=[{'id':'LOW:lighting','severity':'medium','node_id':'smart_panel_01','code':'LOW','category':'environment','summary':'Недостаток света','recommended_steps':['Включить аварийное освещение']}]
        self.a.search=type('Search',(),{'for_issues':lambda *args:[]})()
        self.result={'answer':'Предлагаю включить аварийное освещение','cause':'Недостаток света','confidence':.8,'evidence_issue_ids':['LOW:lighting'],'rag_refs':[],'commands':[cmd(action='line_on',value=7)]}
        async def request(p,purpose,validator):return validator(self.result)
        self.a.llm=type('LLM',(),{'model':'test','chat_active':False,'available':lambda _:True,'request_json':AsyncMock(side_effect=request)})()
        self.out=[];self.chat=Chat(self.a,self.c,queue.Queue(),self.out.append);self.p=Proposals(self.a,self.c,self.chat)
    async def test_prepares_without_executing_and_suppresses_repeat(self):
        await self.p.tick()
        self.assertIsNotNone(self.chat.pending);self.assertIn('Разрешаете',self.out[-1]['text']);self.c.control.assert_not_awaited()
        self.chat.pending=None;self.p.next_check=0;await self.p.tick();self.assertEqual(len(self.out),1)
    async def test_changed_user_intent_blocks_late_plan(self):
        async def reply(p,purpose,validator):self.chat.revision+=1;return validator(self.result)
        self.a.llm.request_json=reply;await self.p.tick();self.assertIsNone(self.chat.pending)
    async def test_changed_situation_blocks_confirmation(self):
        await self.p.tick();plan=self.chat.pending[0];self.a.issues=[]
        with self.assertRaises(ValueError):await self.chat.execute_plan(plan)
        self.c.control.assert_not_awaited()
    async def test_invalid_reference_no_plan(self):
        self.result['evidence_issue_ids']=['fake']
        with self.assertRaises(ValueError):await self.p.tick()
        self.assertIsNone(self.chat.pending)
    async def test_guard_blocks_suggested_poweroff_of_busy_cnc(self):
        self.result['commands']=[cmd(value=4)];await self.p.tick()
        self.assertIsNone(self.chat.pending);self.c.control.assert_not_awaited();self.assertIn('недоступно',self.out[-1]['text'])
    async def test_no_model_local_recommendations_only(self):
        self.a.llm.available=lambda:False;await self.p.tick()
        self.assertIn('локальных правил',self.out[-1]['text']);self.assertIsNone(self.chat.pending)

class Audit(unittest.IsolatedAsyncioTestCase):
    async def test_command_chain_success_and_rejection(self):
        for success in (True,False):
            with tempfile.TemporaryDirectory() as d:
                a,c=factory();a.log=Log(d);a.log.write('start',interface='tkinter')
                async def control(node,action,value):
                    a.frame['timestamp']=101;a.frame['nodes'][node]['updated_at']=101
                    a.frame['nodes'][node]['lines'][3]['state']='ON'
                    return {'success':success}
                c.control=control;chat=Chat(a,c,queue.Queue(),lambda x:None)
                self.assertEqual(await chat.execute(cmd(action='line_on',value=7)),success)
                a.log.write('stop',reason='test');a.log.close()
                rows=[json.loads(s) for s in next(Path(d).glob('*.jsonl')).read_text().splitlines()]
                self.assertEqual(validate(rows),[])
                check=next(x for x in rows if x['kind']=='verification');self.assertEqual(check['ok'],success)
    async def test_real_client_refs_and_llm_fields(self):
        import os
        from unittest.mock import patch
        from dome_agent.client import DomeClient
        from dome_agent.llm import LLM
        with tempfile.TemporaryDirectory() as d:
            log=Log(d);c=DomeClient('ws://example','secret',log,None,None)
            class WS:
                async def send(self,raw):
                    req=json.loads(raw);c.pending[req['request_id']].set_result({'success':True})
            c.ws=WS();await c.request('ping')
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test'}):
                llm=LLM(log);llm._post=lambda _: {'choices':[{'message':{'content':'{"answer":"text"}'}}],'usage':{'prompt_tokens':7,'completion_tokens':2}}
                await llm.request_json({},'chat')
            log.close();rows=[json.loads(s) for s in next(Path(d).glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(validate(rows),[]);self.assertEqual(rows[-1]['prompt_tokens'],7);self.assertEqual(rows[-1]['summary'],'text')
    def test_validator_detects_wrong_ref(self):
        self.assertTrue(validate([{'ts':'2026-01-01T00:00:00.000+03:00','run_id':'r','seq':1,'kind':'verification','ref':1,'expected':{},'observed':{},'ok':False}]))
