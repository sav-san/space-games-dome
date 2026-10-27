import unittest
from test_chat import fixture
from dome_agent.chat import guard, constraints

class PrintStart(unittest.TestCase):
    def test_print_without_home_or_at_nonzero_position(self):
        for homed in (False,None,True):
            a,c,cmd=fixture();a.frame['nodes']['printer_3d_01'].update(homed=homed,x_mm=100,y_mm=50,z_mm=20)
            guard(a,c,cmd)
            self.assertIsNone(next(r for r in constraints(a,c) if r['action']=='start_job')['blocked_reason'])
    def test_manual_move_still_requires_homing(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01']['homed']=False
        cmd.update(action='move',value={'x':1,'mode':'relative','feedrate':100})
        with self.assertRaisesRegex(ValueError,'home'):guard(a,c,cmd)
    def test_occupied_bed_still_blocks_start_without_home(self):
        a,c,cmd=fixture();a.frame['nodes']['printer_3d_01'].update(homed=False,bed_clear=False)
        with self.assertRaisesRegex(ValueError,'Стол'):guard(a,c,cmd)
