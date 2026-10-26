import unittest
from pathlib import Path
from dome_agent.core import Search
from dome_agent.trends import Trends

ROOT=Path(__file__).resolve().parents[1]
class NewKnowledge(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.search=Search(ROOT/'knowledge/chunks.jsonl')
 def test_retrieval_cases(self):
  pairs=[('замок не открывается CRC пустые показания бокса','Шина бокса'),('вытяжка снова перегревается MOTOR_OVERHEAT','Повторный перегрев'),('leakage_ma растёт ЧПУ УЗО','Ток утечки'),('вода в топливе дизель не запускается','Вода в топливе'),('вентилятор инвертора ток ноль температура растёт','Деградация вентилятора'),('низкий CO2 99 другие датчики 1000','CO2: реальный'),('E07 при нагреве стола и работе фрезера','E07 при одновременных')]
  for q,expected in pairs:
   with self.subTest(query=q):self.assertTrue(any(expected in x['text'] for x in self.search.search(q,3)))
 def test_case_has_original_evidence(self):
  hits=self.search.search('замок не открывается CRC пустые показания бокса',5)
  self.assertTrue(any(h['metadata'].get('document_role') in ('event_evidence','telemetry_profile') for h in hits))
 def test_multiple_incidents_retrieval(self):
  issues=[{'node_id':'fuel_tank_01','code':'FUEL_WATER','summary':'вода в топливе дизель не запускается'}, {'node_id':'fume_extraction_01','code':'MOTOR_OVERHEAT','summary':'вытяжка повторно перегревается'}]
  hits=self.search.for_issues(issues)
  self.assertTrue(any('Вода в топливе' in h['text'] for h in hits));self.assertTrue(any('MOTOR_OVERHEAT' in h['text'] for h in hits))

class TemporalHints(unittest.TestCase):
 def n(self,**kw):return {'online':True,'status':'OK',**kw}
 def test_accumulated_crc_is_not_new_failure(self):
  t=Trends();n={'thermal_insulation_01':self.n(crc_errors=1725)}
  t.analyze(n,100,[]);self.assertFalse(t.analyze(n,160,[]))
  n={'thermal_insulation_01':self.n(crc_errors=1800)}
  self.assertEqual(t.analyze(n,180,[])[0]['code'],'CRC_GROWTH')
 def test_frozen_node_ignored(self):
  t=Trends();n={'solar_inverter_01':self.n(fan_state='FAULT')}
  self.assertEqual(t.analyze(n,100,[{'code':'STALE','node_id':'solar_inverter_01'}]),[])
 def test_soc_jump_and_gap_reset(self):
  t=Trends();t.analyze({'battery_01':self.n(soc_pct=78)},100,[])
  self.assertEqual(t.analyze({'battery_01':self.n(soc_pct=20)},160,[])[0]['code'],'SOC_JUMP')
  self.assertEqual(t.analyze({'battery_01':self.n(soc_pct=80)},400,[]),[])
 def test_new_link_flaps(self):
  t=Trends();t.analyze({'network_link_01':self.n(link_flaps=30)},100,[])
  self.assertFalse(t.analyze({'network_link_01':self.n(link_flaps=30)},160,[]))
  self.assertEqual(t.analyze({'network_link_01':self.n(link_flaps=31)},180,[])[0]['code'],'LINK_FLAPS')
