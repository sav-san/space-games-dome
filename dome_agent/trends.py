"""Bounded history for diagnostic hints; does not execute controls."""
from collections import deque
from .core import number

class Trends:
    def __init__(self):self.samples=deque(maxlen=400);self.last=None
    def analyze(self,nodes,stamp,issues):
        if self.last is not None and stamp<=self.last:return []
        if self.last is not None and stamp-self.last>90:self.samples.clear()
        self.last=stamp
        bad={x['node_id'] for x in issues if x['code'] in ('STALE','MISSING')}
        clean={k:v for k,v in nodes.items() if k not in bad and v.get('online') is True and v.get('status')=='OK'}
        self.samples.append((stamp,clean))
        while self.samples and stamp-self.samples[0][0]>600:self.samples.popleft()
        old=next(((t,n) for t,n in self.samples if 30<=stamp-t<=600),None)
        out=[]
        def add(code,node,category,severity,title,evidence,steps):
            out.append({'id':code+':'+node,'code':code,'node_id':node,'category':category,'severity':severity,'summary':title,'evidence':evidence,'recommended_steps':steps,'certainty':'observed_symptom','cause':'Причина требует проверки'})
        for node,n in clean.items():
            prev=(old[1].get(node) or {}) if old else {}
            if node=='solar_inverter_01' and n.get('fan_state')=='FAULT':
                add('FAN_FAULT',node,'equipment_fault','high','Неисправность вентилятора инвертора', {k:n.get(k) for k in ('fan_state','fan_current_a','inverter_temp_c')},['Проверить ток вентилятора и динамику температуры; предложить осмотр и согласованное снижение нагрузки.'])
            if node=='fuel_tank_01' and n.get('fuel_quality')=='WATER':
                add('FUEL_WATER',node,'equipment_fault','medium','Вода в топливе: риск отказа запуска дизеля', {k:n.get(k) for k in ('fuel_quality','water_content_ppm','reserve_l')},['Сверить качество топлива и допустимый порог, запросить обслуживание; не повторять запуск без проверки причины.'])
            if node.startswith('thermal_insulation') and any(v is None or (isinstance(v,list) and any(x is None for x in v)) for v in n.values()):
                add('BOX_NULL',node,'telemetry_quality','medium','Пустые показания бокса; проверить шину и CRC',{'crc_errors':n.get('crc_errors'),'poll_interval_ms':n.get('poll_interval_ms')},['Сравнить рост CRC, связь и пустые значения; запросить осмотр шины, не считать старый счётчик новой аварией.'])
            if old and node.startswith('thermal_insulation') and number(n.get('crc_errors')) and number(prev.get('crc_errors')) and n['crc_errors']>prev['crc_errors']:
                add('CRC_GROWTH',node,'communication','medium','Растёт число CRC-ошибок шины',{'before':prev['crc_errors'],'after':n['crc_errors'],'window_s':stamp-old[0]},['Сопоставить пропуски и команды без эффекта, проверить соединения и параметры опроса.'])
            if old and node=='battery_01' and number(n.get('soc_pct')) and number(prev.get('soc_pct')) and abs(n['soc_pct']-prev['soc_pct'])>=15:
                add('SOC_JUMP',node,'conflicting_data','high','Резкое изменение SOC батареи',{'before':prev['soc_pct'],'after':n['soc_pct'],'window_s':stamp-old[0],'temperature_c':n.get('temperature_c'),'heuristic_delta_pct':15},['Сверить BMS, инвертор, напряжение и ток; холод или ошибка BMS остаются гипотезами.'])
            if old and node=='network_link_01' and number(n.get('link_flaps')) and number(prev.get('link_flaps')) and n['link_flaps']>prev['link_flaps']:
                add('LINK_FLAPS',node,'communication','high','Появились новые обрывы основного канала',{'before':prev['link_flaps'],'after':n['link_flaps'],'rx_power_dbm':n.get('rx_power_dbm'),'reserve_link_state':n.get('reserve_link_state')},['Проверить оптический тракт и доступность резерва; согласовать физическое переключение с оператором.'])
            if old and node=='smart_panel_01':
                previous={x.get('line'):x for x in prev.get('lines',[]) if isinstance(x,dict)}
                for line in n.get('lines',[]):
                    if not isinstance(line,dict):continue
                    before=previous.get(line.get('line'),{}).get('leakage_ma');after=line.get('leakage_ma')
                    if number(before) and number(after) and after>=10 and after-before>=3:
                        add('LEAKAGE_RISE_'+str(line.get('line')),node,'power_distribution','high','Растёт ток утечки линии '+str(line.get('name',line.get('line'))),{'line':line.get('line'),'before_ma':before,'after_ma':after,'window_s':stamp-old[0],'heuristic':True},['Запросить осмотр кабеля до срабатывания защиты. Пороги 10 мА и рост 3 мА — диагностическая эвристика, не уставка УЗО.'])
        climate={k:v for k,v in clean.items() if k.startswith('climate_sensor') and number(v.get('co2_ppm'))}
        if len(climate)>1:
            vals=[v['co2_ppm'] for v in climate.values()]
            if min(vals)<250 and max(vals)>700 and max(vals)-min(vals)>500:
                add('LOW_CO2_CONFLICT','climate','conflicting_data','high','Подозрительно низкий CO₂ при расхождении датчиков',{k:{f:v.get(f) for f in ('co2_ppm','reading_delay_s','sensor_state')} for k,v in climate.items()},['Проверить независимые измерения и задержки, расход притока и фильтр. Большинство датчиков не доказательство.'])
        return out
