import unittest
from types import SimpleNamespace as NS
from test_chat import fixture
from dome_agent.chat import guard
from dome_agent.core import Detector,node_time_error

class Freshness(unittest.TestCase):
    def test_age_16_now_matches_detector(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01']['updated_at']=84
        a.detector=Detector(20);a.issues=a.detector.analyze(a.frame['nodes'],100)
        guard(a,c,cmd)
    def test_custom_threshold(self):
        a,c,cmd=fixture();a.detector=Detector(12);a.frame['nodes']['printer_3d_01']['updated_at']=84
        with self.assertRaisesRegex(ValueError,'16.000 с > 12'):guard(a,c,cmd)
    def test_small_timestamp_skew(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01']['updated_at']=100.1;guard(a,c,cmd)
    def test_large_skew_rejected_by_both(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01']['updated_at']=110
        with self.assertRaisesRegex(ValueError,'опережает снимок'):guard(a,c,cmd)
        self.assertEqual(Detector().analyze(a.frame['nodes'],100)[0]['code'],'STALE')
    def test_old_snapshot_detailed(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01']['updated_at']=70
        with self.assertRaisesRegex(ValueError,'updated_at=70'):guard(a,c,cmd)
    def test_offline_not_bypassed(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01']['online']=False
        with self.assertRaisesRegex(ValueError,'online=False'):guard(a,c,cmd)
    def test_lighting_on_off_both(self):
        for action in ('line_on','line_off'):
            a,c,_=fixture();a.frame['nodes']={'smart_panel_01':{'online':True,'status':'OK','updated_at':84,'lines':[{'line':6,'name':'Освещение','state':'OFF'}]}}
            c.catalog=[{'node_id':'smart_panel_01','controls':['line_on','line_off']}]
            guard(a,c,{'node_id':'smart_panel_01','action':action,'value':6})
    def test_unknown_still_blocked(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01']['status']='UNKNOWN'
        with self.assertRaisesRegex(ValueError,'UNKNOWN'):guard(a,c,cmd)
