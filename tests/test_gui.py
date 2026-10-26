import tempfile
import time
import unittest
from pathlib import Path
from gui import flatten, notifications
from main import Agent, ROOT
from dome_agent.core import Log

class GuiData(unittest.TestCase):
    def test_all_nested_values(self):
        rows=dict(flatten({'a': {'b': [1,None]}, 'empty':{},'flag':False}))
        self.assertEqual(rows,{'a.b[0]':'1','a.b[1]':'null','empty':'{}','flag':'false'})
    def test_stale_hides_causes(self):
        self.assertNotIn('TEST_CAUSE',notifications({'fresh':False,'diagnosis':{'summary':'TEST_CAUSE'}}))
    def test_causes_and_checks(self):
        text=notifications({'fresh':True,'issues':[], 'diagnosis':{'summary':'summary','hypotheses':[{'cause':'TEST_CAUSE','evidence_issue_ids':['a'],'checks':['CHECK'],'proposed_steps':['STEP'],'rag_refs':['ref']}]}})
        for word in ('TEST_CAUSE','CHECK','STEP','ref'):self.assertIn(word,text)
    def test_snapshot_and_disconnect(self):
        with tempfile.TemporaryDirectory() as d:
            log=Log(d)
            try:
                agent=Agent(log,ROOT/'knowledge/chunks.jsonl',d);out=[];agent.ui_callback=out.append
                agent.status('team',1)
                nodes={'test':{'online':True,'status':'OK','updated_at':100,'custom':{'x':[1,2]}}}
                agent.receive({'timestamp':100,'data':{'nodes':nodes}},1)
                self.assertEqual(out[-1]['frame']['nodes'],nodes)
                self.assertTrue(out[-1]['fresh'])
                agent.ui_diagnosis={'summary':'cause'};agent.ui_signature=agent.signature;agent.ui_diagnosis_time=time.monotonic()
                agent.emit_ui();self.assertIsNotNone(out[-1]['diagnosis'])
                agent.last_received-=20;agent.emit_ui();self.assertFalse(out[-1]['fresh']);self.assertIsNone(out[-1]['diagnosis'])
                agent.status('disconnected',1);self.assertIsNone(out[-1]['frame'])
            finally:log.close()
