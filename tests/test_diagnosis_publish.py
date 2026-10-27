import sys
import tempfile
import time
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from main import Agent
from dome_agent.core import Log
from dome_agent.llm import LLM


def make(d):
    log=Log(d);a=Agent(log,ROOT/'knowledge/chunks.jsonl',d);a.status('team',1);return log,a


class FlappingSignature(unittest.TestCase):
    def test_diagnosis_survives_changed_signature_but_hides_stale_hypotheses(self):
        with tempfile.TemporaryDirectory() as d:
            log,a=make(d)
            try:
                a.issues=[{'id':'STALE:x','severity':'high'},{'id':'NULL_co2_ppm:y','severity':'medium'}]
                a.signature=(('STALE:x','high'),)  # набор уже изменился после запуска анализа
                a.ui_signature=(('old','high'),)
                a.ui_diagnosis={'summary':'s','hypotheses':[
                    {'cause':'A','evidence_issue_ids':['STALE:x']},
                    {'cause':'B','evidence_issue_ids':['GONE:z']}]}
                a.ui_diagnosis_time=time.monotonic()
                cur=a.current_diagnosis()
                self.assertEqual([h['cause'] for h in cur['hypotheses']],['A']);self.assertTrue(cur['partial'])
            finally:log.close()

    def test_all_hypotheses_stale_means_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            log,a=make(d)
            try:
                a.issues=[{'id':'NEW:n','severity':'high'}];a.signature=(('NEW:n','high'),);a.ui_signature=(('old','high'),)
                a.ui_diagnosis={'summary':'s','hypotheses':[{'cause':'B','evidence_issue_ids':['GONE:z']}]};a.ui_diagnosis_time=time.monotonic()
                self.assertIsNone(a.current_diagnosis())
            finally:log.close()

    def test_exact_signature_unchanged_and_expires(self):
        with tempfile.TemporaryDirectory() as d:
            log,a=make(d)
            try:
                a.signature=a.ui_signature=();a.ui_diagnosis={'summary':'s'};a.ui_diagnosis_time=time.monotonic()
                self.assertIs(a.current_diagnosis(),a.ui_diagnosis)
                a.ui_diagnosis_time-=121;self.assertIsNone(a.current_diagnosis())
            finally:log.close()


class Sanitize(unittest.TestCase):
    def test_bad_service_fields_are_repaired(self):
        raw={'summary':'s','hypotheses':[
            {'category':'power','cause':'a','confidence':85,'evidence_issue_ids':['x','ghost'],'rag_refs':['ghost'],'checks':['c',5],'proposed_steps':None},
            {'category':'unknown','cause':'b','confidence':.5,'evidence_issue_ids':['ghost'],'rag_refs':[],'checks':[],'proposed_steps':[]}]}
        out=LLM.validate(LLM.sanitize(raw,{'x'},{'r'}),{'x'},{'r'})
        self.assertEqual(len(out['hypotheses']),1);h=out['hypotheses'][0]
        self.assertEqual((h['category'],h['confidence'],h['evidence_issue_ids'],h['rag_refs'],h['checks'],h['proposed_steps']),('unknown',.85,['x'],[],['c'],[]))


class Thinking(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_disabled_by_default(self):
        import os
        os.environ['DEEPSEEK_API_KEY']='k'
        with tempfile.TemporaryDirectory() as d:
            log=Log(d);llm=LLM(log);seen=[]
            llm._post=lambda p:(seen.append(p),{'choices':[{'finish_reason':'stop','message':{'content':'{"a":1}'}}]})[1]
            await llm.request_json({'model':'m','messages':[]},'chat')
            os.environ['DEEPSEEK_THINKING']='default';p2={'model':'m','messages':[]}
            await llm.request_json(p2,'chat');del os.environ['DEEPSEEK_THINKING']
            log.close()
        del os.environ['DEEPSEEK_API_KEY']
        self.assertEqual(seen[0]['thinking'],{'type':'disabled'});self.assertNotIn('thinking',seen[1])


class Salvage(unittest.IsolatedAsyncioTestCase):
    def test_broken_json_keeps_good_hypotheses(self):
        from dome_agent.llm import salvage_diagnosis
        bad='{"summary":"Итог","hypotheses":[{"category":"energy","cause":"A","confidence":0.5,"evidence_issue_ids":["x"],"rag_refs":[],"checks":[],"proposed_steps":[]},{"category":"energy","cause":"код "E21" сломал","confidence":0.4,"evidence_issue_ids":["x"] "rag_refs":[],"checks":[],"proposed_steps":[]},{"category":"unknown","cause":"C","confidence":0.2,"evidence_issue_ids":["x"],"rag_refs":[],"checks":[],"proposed_steps":[]}'
        out=salvage_diagnosis(bad)
        self.assertEqual([h['cause'] for h in out['hypotheses']],['A','C']);self.assertIn('частично',out['summary'])
        with self.assertRaises(ValueError):salvage_diagnosis('мусор без json')

    async def test_diagnose_uses_salvage(self):
        import os
        os.environ['DEEPSEEK_API_KEY']='k'
        with tempfile.TemporaryDirectory() as d:
            log=Log(d);llm=LLM(log)
            bad='{"summary":"S","hypotheses":[{"category":"energy","cause":"A","confidence":0.5,"evidence_issue_ids":["x"],"rag_refs":[],"checks":[],"proposed_steps":[]},{"cause":"сломано" "x"}]}'
            llm._post=lambda p:{'choices':[{'finish_reason':'stop','message':{'content':bad}}]}
            r=await llm.diagnose([{'id':'x','node_id':'n','code':'c','summary':'s'}],[],[])
            log.close()
        del os.environ['DEEPSEEK_API_KEY']
        self.assertEqual(len(r['hypotheses']),1);self.assertEqual(llm.last_diagnosis_error,None)


if __name__=='__main__':unittest.main()
