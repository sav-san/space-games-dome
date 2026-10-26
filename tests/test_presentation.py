import unittest
from dome_agent.presentation import records,group,field_title,values_flat

class Tables(unittest.TestCase):
    def data(self,nodes,issues=None,catalog=None):return {'fresh':True,'frame':{'nodes':nodes},'issues':issues or [],'catalog':catalog or []}
    def test_categories(self):
        for node,expected in [('printer_3d_01','Принтеры и ЧПУ'),('cnc_01','Принтеры и ЧПУ'),('climate_sensor_02','Датчики'),('battery_01','Энергетика'),('water_pump_01','Вода'),('new_01','Прочее')]:self.assertEqual(group(node),expected)
    def test_catalog_title(self):
        row=records(self.data({'climate_sensor_01':{'co2_ppm':400}},catalog=[{'node_id':'climate_sensor_01','title':'Климат склада'}]))[0]
        self.assertEqual(row['name'],'climate_sensor_01 (Климат склада)')
    def test_worst_severity(self):
        issues=[{'node_id':'battery_01','severity':x} for x in ('low','high','medium')]
        self.assertEqual(records(self.data({'battery_01':{}},issues))[0]['severity'],'high')
    def test_climate_conflict(self):
        rows=records(self.data({'climate_sensor_01':{},'climate_sensor_02':{}},[{'node_id':'climate','severity':'high','evidence':{'climate_sensor_02':{}}}]))
        self.assertEqual(rows[0]['severity'],'');self.assertEqual(rows[1]['severity'],'high')
    def test_lighting_actual_names(self):
        data=self.data({'smart_panel_01':{'lines':[{'line':4,'name':'Освещение обычное','state':'ON'},{'line':5,'name':'Принтер','state':'OFF'}]}},[{'node_id':'smart_panel_01','code':'LINE_4','severity':'high','evidence':{'line':4}}])
        rows=records(data);lights=[r for r in rows if r['group']=='Освещение']
        self.assertEqual(len(lights),1);self.assertIn('линия 4',lights[0]['name']);self.assertEqual(lights[0]['severity'],'high')
    def test_stale_removes_colors(self):
        d=self.data({'battery_01':{'soc_pct':10}},[{'node_id':'battery_01','severity':'high'}]);d['fresh']=False
        row=records(d)[0];self.assertEqual(row['severity'],'');self.assertEqual(row['state'],'Нет актуальных данных')
    def test_all_nested_fields(self):
        values=dict(values_flat({'a':[{'b':None}],'empty':{},'bool':False}))
        self.assertEqual(set(values),{'a[0].b','empty','bool'});self.assertIn('null',values['a[0].b'])
        self.assertIn('Температура',field_title('temperature_c'))
    def test_missing_visible(self):
        row=records(self.data({},[{'node_id':'cnc_01','code':'MISSING','severity':'high'}]))[0]
        self.assertEqual(row['node'],'cnc_01');self.assertEqual(row['severity'],'high')
