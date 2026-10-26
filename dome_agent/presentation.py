"""Display-only grouping. Never used to resolve control commands."""
import json
import re

GROUPS=('Все','Датчики','Освещение','Принтеры и ЧПУ','Энергетика','Климат и вентиляция','Вода','Безопасность','Связь и автоматика','Прочее')
NAMES={
 'solar_panels_01':'Солнечные панели','solar_inverter_01':'Солнечный инвертор','battery_01':'Аккумулятор',
 'wind_turbine_01':'Ветрогенератор','dizel_1':'Дизель-генератор','fuel_tank_01':'Топливный бак','smart_panel_01':'Распределительный щит',
 'printer_3d_01':'3D-принтер','cnc_01':'Фрезерный станок ЧПУ','material_inventory_01':'Склад материалов','equipment_cooling_01':'Охлаждение станков',
 'thermal_insulation_01':'Бокс ФабЛаб','thermal_insulation_02':'Бокс Химлаб',
 'climate_sensor_01':'Основной датчик климата ФабЛаб','climate_sensor_02':'Дополнительный датчик климата №2','climate_sensor_03':'Дополнительный датчик климата №3',
 'air_quality_sensor_01':'Датчик качества воздуха CO/VOC','smoke_detector_01':'Датчик дыма №1','smoke_detector_02':'Датчик дыма №2',
 'radiation_sensor_01':'Датчик радиации','chem_sensor_01':'Химический датчик','weather_station_01':'Метеостанция','seysmo_01':'Датчик сейсмической активности',
 'fume_extraction_01':'Вытяжка ФабЛаб','supply_ventilation_01':'Приточная вентиляция','dome_sealing_01':'Герметизация и клапаны',
 'water_pump_01':'Насосная станция','water_tank_01':'Резервуар воды','water_filter_01':'Водоочистка',
 'fire_suppression_01':'Пожаротушение','access_control_01':'Двери и контроль доступа',
 'network_link_01':'Основной сетевой канал','radio_01':'Радиосвязь','backup_comms_01':'Резервная связь','edge_compute_01':'Вычислительный узел','dome_automation_01':'Контроллер автоматики','meeting_01':'Чат Купола'}
FIELDS={'online':'На связи','status':'Статус данных','state':'Состояние','updated_at':'Время обновления API',
 'temperature_c':'Температура, °C','humidity_pct':'Влажность, %','co2_ppm':'Углекислый газ, ppm','co_ppm':'Угарный газ, ppm','voc_index':'Индекс VOC',
 'power_w':'Мощность, Вт','voltage_v':'Напряжение, В','current_a':'Ток, А','soc_pct':'Заряд АКБ, %','progress_pct':'Выполнение, %',
 'nozzle_temp_c':'Температура сопла, °C','bed_temp_c':'Температура стола, °C','nozzle_target_c':'Заданная температура сопла, °C','bed_target_c':'Заданная температура стола, °C',
 'file_name':'Имя задания','hold_reason':'Причина удержания','hold_level':'Уровень удержания','error_code':'Код ошибки','alarm_code':'Код аварии',
 'airflow_m3_h':'Расход воздуха, м³/ч','level_pct':'Уровень, %','pressure_bar':'Давление, бар','speed_pct':'Скорость, %',
 'x_mm':'Координата X, мм','y_mm':'Координата Y, мм','z_mm':'Координата Z, мм','homed':'Поиск исходной позиции выполнен','bed_clear':'Стол свободен',
 'smoke_detected':'Обнаружен дым','dose_rate_usv_h':'Мощность дозы, мкЗв/ч','concentration_ppm':'Концентрация, ppm','sensor_state':'Состояние датчика',
 'reading_delay_s':'Задержка измерения, с','confidence':'Достоверность','name':'Название','line':'Номер линии','alarm':'Авария','lockout_remaining_s':'Остаток блокировки, с'}
RANK={'critical':0,'high':1,'medium':2,'low':3}
LABEL={'critical':'Критическая','high':'Высокая','medium':'Средняя','low':'Предупреждение','':'Нет признаков'}

def group(node,meta=None):
    hint=(node+' '+str((meta or {}).get('category',''))+' '+str((meta or {}).get('title',''))).lower()
    if any(x in hint for x in ('lighting','освещ','light_')):return 'Освещение'
    if any(x in node for x in ('sensor','detector','weather','seysmo')):return 'Датчики'
    if node.startswith(('printer','cnc','equipment_cooling','material_inventory')):return 'Принтеры и ЧПУ'
    if node.startswith(('solar','battery','wind','dizel','fuel','smart_panel')):return 'Энергетика'
    if node.startswith(('thermal','fume','supply_ventilation','dome_sealing')):return 'Климат и вентиляция'
    if node.startswith('water'):return 'Вода'
    if node.startswith(('fire','access')):return 'Безопасность'
    if node.startswith(('network','radio','backup_comms','edge','dome_automation','meeting')):return 'Связь и автоматика'
    # Unknown nodes remain visible; only explicit catalog classification is used.
    if any(x in hint for x in ('sensor','датчик')):return 'Датчики'
    return 'Прочее'

def field_title(path):
    leaf=re.sub(r'\[\d+\]','',path.split('.')[-1])
    return f'{path} ({FIELDS[leaf]})' if leaf in FIELDS else path

def values_flat(value,path=''):
    if isinstance(value,dict) and value:
        for k,v in value.items():yield from values_flat(v,f'{path}.{k}' if path else str(k))
    elif isinstance(value,list) and value:
        for i,v in enumerate(value):yield from values_flat(v,f'{path}[{i}]')
    else:yield path, 'Нет данных (null)' if value is None else 'Да (true)' if value is True else 'Нет (false)' if value is False else value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)

def records(data):
    nodes=(data.get('frame') or {}).get('nodes',{});fresh=data.get('fresh',False)
    catalog={n['node_id']:n for n in data.get('catalog',[]) if isinstance(n,dict) and isinstance(n.get('node_id'),str)}
    issues=data.get('issues',[]) if fresh else []
    missing={i['node_id'] for i in issues if i.get('code')=='MISSING'}
    result=[]
    def make(key,node,values,title,category,related):
        severity=min((i.get('severity','') for i in related),key=lambda x:RANK.get(x,4),default='')
        fields=list(values_flat(values))
        metrics=[]
        for k in ('temperature_c','humidity_pct','co2_ppm','soc_pct','power_w','progress_pct','level_pct','airflow_m3_h'):
            if k in values:metrics.append(f'{FIELDS.get(k,k)}: {values[k]}')
        return {'id':key,'node':node,'name':f'{key} ({title})','group':category,'fields':fields,'severity':severity,
                'state':str(values.get('state',values.get('status','—'))) if fresh and values else 'Нет актуальных данных',
                'level':LABEL[severity] if fresh and values else 'Нет актуальных данных','summary':'; '.join(metrics),
                'issues':'; '.join(i.get('summary','') for i in related)}
    for node in sorted(set(nodes)|missing):
        values=nodes.get(node,{})
        if not isinstance(values,dict):continue
        related=[i for i in issues if i.get('node_id')==node or (i.get('node_id')=='climate' and node in i.get('evidence',{}))]
        title=catalog.get(node,{}).get('title') or NAMES.get(node,'Неизвестный узел')
        result.append(make(node,node,values,title,group(node,catalog.get(node)),related))
        if node=='smart_panel_01':
            for idx,line in enumerate(values.get('lines',[]) or []):
                if not isinstance(line,dict):continue
                name=str(line.get('name',''))
                if not any(x in name.lower() for x in ('освещ','lighting','light')):continue
                # Derive lighting from actual line names, never a fixed line number.
                key=f'{node}.lines[{idx}]'
                line_issues=[i for i in related if i.get('code') in ('STALE','MISSING') or str(i.get('evidence',{}).get('line'))==str(line.get('line'))]
                result.append(make(key,node,line,name+'; линия '+str(line.get('line','?')),'Освещение',line_issues))
    return result
